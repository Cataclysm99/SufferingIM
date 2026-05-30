"""Slash command registration for playback, library management, and persona controls."""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
import string
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from yt_dlp.utils import DownloadError

from app_bot import MusicBot, _help_manager_tutorial_embed, _help_tutorial_embed, _unique_path
from broadcast import DAYS as DJ_DAYS
from config import ADS_DIR, ALLOWED_EXTENSIONS, DJ_EVENTS_DIR, SONGS_DIR
from database import (
    activate_song,
    add_ad,
    add_broadcast,
    add_song,
    get_all_ads_admin,
    get_all_broadcasts_admin,
    deactivate_song,
    get_all_songs,
    get_all_songs_admin,
    get_songs_by_name,
    purge_all_songs,
    search_songs,
    sync_ads_from_disk,
    sync_broadcasts_from_disk,
)
from media_utils import download_youtube_audio, extract_urls, is_youtube_url
from views import (
    REACT_CANCEL,
    REACT_DEACTIVATE,
    REACT_HARD_DELETE,
    SongListView,
    _build_delete_confirm_message,
    _get_song_or_respond_missing,
    _song_table_embed,
    is_music_manager,
    require_music_manager,
)
from views.helpers import _fit_rows_to_embed

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _UploadRequest:
    """Normalized upload request assembled from slash-command options."""

    source: str | None
    file: discord.Attachment | None
    name: str | None
    credit: str | None
    kind: str
    day: str | None = None
    slot: str | None = None


class PurgeSongsConfirmModal(discord.ui.Modal, title="Confirm Full Song Purge"):
    """Confirm the destructive full-library purge using a terminal password."""

    password = discord.ui.TextInput(
        label="Terminal confirmation password",
        placeholder="Check the bot terminal output for the password",
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        """Validate the password and purge all songs from disk and the database."""
        client = interaction.client  # type: ignore[attr-defined]
        if not client.purge_code:
            await interaction.response.send_message(
                "❌ No active purge session. Run `/purge_songs` again.",
                ephemeral=True,
            )
            return
        if self.password.value.strip() != client.purge_code:
            await interaction.response.send_message(
                "❌ Incorrect password. Purge aborted.",
                ephemeral=True,
            )
            return
        client.purge_code = None
        await interaction.response.defer(ephemeral=True)

        deleted = purge_all_songs()
        deleted_files = 0
        for path in SONGS_DIR.iterdir():
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                path.unlink()
                deleted_files += 1
            except OSError as exc:
                log.warning("Could not delete file %s during purge: %s", path, exc)

        log.warning(
            "PURGE executed by user %s: %d DB records deleted, %d files removed.",
            interaction.user.id,
            len(deleted),
            deleted_files,
        )
        await interaction.followup.send(
            "🗑️ Purged "
            f"**{len(deleted)}** song(s) from the database and removed "
            f"**{deleted_files}** file(s).",
            ephemeral=True,
        )


def _attachment_extension(file: discord.Attachment | None) -> str:
    """Return the lowercase file extension for an attachment."""
    if file is None or "." not in file.filename:
        return ""
    return "." + file.filename.rsplit(".", 1)[-1].lower()


async def _validate_upload_request(
    interaction: discord.Interaction,
    request: _UploadRequest,
) -> bool:
    """Validate permissions and upload inputs before starting work."""
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to upload media.",
            ephemeral=True,
        )
        return False

    youtube_urls = [
        url for url in extract_urls(request.source or "") if is_youtube_url(url)
    ]
    if request.file is None and not youtube_urls:
        await interaction.response.send_message(
            "❌ Provide an attachment, a YouTube link, or both.",
            ephemeral=True,
        )
        return False

    extension = _attachment_extension(request.file)
    if request.file is not None and extension not in ALLOWED_EXTENSIONS:
        await interaction.response.send_message(
            f"❌ Unsupported file type `{extension}`.\n"
            f"Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
            ephemeral=True,
        )
        return False
    return True


def _broadcast_target_day(request: _UploadRequest) -> str:
    """Resolve the destination weekday for a broadcast upload."""
    if request.day:
        return request.day
    return datetime.datetime.now(datetime.timezone.utc).strftime("%A").lower()


def _parse_broadcast_placement(placement: str | None) -> tuple[str | None, str | None]:
    """Parse `day[:slot]` placement text for broadcast uploads."""
    if not placement:
        return None, None
    chunks = [part.strip().lower() for part in placement.split(":", maxsplit=1)]
    day = chunks[0] if chunks and chunks[0] in DJ_DAYS else None
    slot = None
    if len(chunks) == 2 and chunks[1] in {"intro", "outro", "event"}:
        slot = chunks[1]
    return day, slot


def _target_dir(request: _UploadRequest) -> Path:
    """Return the destination directory for upload storage."""
    if request.kind == "song":
        return SONGS_DIR
    if request.kind == "ad":
        return ADS_DIR
    day_dir = DJ_EVENTS_DIR / _broadcast_target_day(request)
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir


def _store_media_record(
    interaction: discord.Interaction,
    request: _UploadRequest,
    file_path: Path,
    *,
    fallback_credit: str | None = None,
) -> int:
    """Persist one uploaded media file in its database table and return record ID."""
    display_name = (request.name or file_path.stem).strip() or file_path.stem
    credit = (request.credit or fallback_credit or "Unknown").strip() or "Unknown"
    added_by = str(interaction.user.id)
    if request.kind == "song":
        return add_song(display_name, credit, file_path.name, added_by)
    if request.kind == "ad":
        return add_ad(display_name, credit, file_path.name, added_by)
    slot = (request.slot or "event").strip().lower()
    return add_broadcast(
        {
            "name": display_name,
            "sponsor": credit,
            "added_by": added_by,
            "day": _broadcast_target_day(request),
            "slot": slot,
            "filename": file_path.name,
        }
    )


async def _store_downloaded_media(
    interaction: discord.Interaction,
    request: _UploadRequest,
) -> tuple[list[int], list[str], list[str]]:
    """Download and store media from YouTube URLs."""
    urls = extract_urls(request.source or "")
    youtube_urls = [url for url in urls if is_youtube_url(url)]
    inserted_ids: list[int] = []
    failed_urls: list[str] = []
    target_dir = _target_dir(request)

    for url in youtube_urls:
        try:
            downloaded, playlist_title = await asyncio.to_thread(
                download_youtube_audio,
                url,
                target_dir,
                False,
            )
        except (DownloadError, OSError) as exc:
            log.warning("yt-dlp download failed for %s: %s", url, exc)
            failed_urls.append(url)
            continue

        for path in downloaded:
            try:
                inserted_ids.append(
                    _store_media_record(
                        interaction,
                        request,
                        path,
                        fallback_credit=playlist_title if request.kind == "song" else None,
                    )
                )
            except ValueError:
                failed_urls.append(url)
                continue

    ignored_urls = [url for url in urls if url not in youtube_urls]
    return inserted_ids, ignored_urls, failed_urls


async def _store_uploaded_attachment(
    interaction: discord.Interaction,
    request: _UploadRequest,
) -> tuple[list[int], str | None]:
    """Store an uploaded attachment in the selected media library."""
    if request.file is None:
        return [], None

    target_dir = _target_dir(request)
    destination = _unique_path(target_dir, request.file.filename)
    await request.file.save(destination)
    try:
        record_id = _store_media_record(interaction, request, destination)
    except ValueError:
        if destination.exists():
            destination.unlink()
        return [], "❌ Invalid reserved filename; upload was skipped."
    return [record_id], None


def _upload_summary(
    request: _UploadRequest,
    inserted_ids: list[int],
    ignored_urls: list[str],
    failed_urls: list[str],
) -> str:
    """Build the final upload status message."""
    lines: list[str] = []
    media_name = request.kind
    if inserted_ids:
        lines.append(
            "✅ Added "
            f"{len(inserted_ids)} {media_name}(s). IDs: `{', '.join(map(str, inserted_ids))}`"
        )
    if ignored_urls:
        lines.append(f"⚠️ Ignored {len(ignored_urls)} non-YouTube URL(s).")
    if failed_urls:
        lines.append(f"⚠️ Failed to download {len(failed_urls)} YouTube URL(s).")
    if not lines:
        lines.append("❌ No media was added.")
    return "\n".join(lines)


async def _handle_upload_song(
    interaction: discord.Interaction,
    request: _UploadRequest,
) -> str:
    """Process a validated upload request and return the status message."""
    inserted_ids, ignored_urls, failed_urls = await _store_downloaded_media(
        interaction,
        request,
    )
    file_ids, error_message = await _store_uploaded_attachment(interaction, request)
    if error_message is not None:
        return error_message

    inserted_ids.extend(file_ids)
    if request.kind == "ad" and inserted_ids:
        sync_ads_from_disk()
    if request.kind == "broadcast" and inserted_ids:
        sync_broadcasts_from_disk()
    return _upload_summary(request, inserted_ids, ignored_urls, failed_urls)


def _register_controller_commands(bot: MusicBot) -> None:
    """Register general controller and tutorial commands."""

    @bot.tree.command(name="controller", description="Post the controller panel.")
    async def cmd_controller(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=bot.active_controller_embed(),
            view=bot.active_controller_view(),
        )

    @bot.tree.command(
        name="helpless", description="DM user a quick tutorial and command list."
    )
    async def cmd_helpless(interaction: discord.Interaction) -> None:
        try:
            await interaction.user.send(embed=_help_tutorial_embed())
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Asks for help but has DMs closed? How am I supposed to teach you anything? "
                "Fix your settings and try again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Oh look, a helpless case, how surprising. :rolling_eyes: I've sent you the tutorial, "
            "but don't expect me to spell everything out for you. "
            "Explore the commands and figure it out yourself. 😒",
            ephemeral=True,
        )

    @bot.tree.command(
        name="helpless_manager",
        description="DM user a quick tutorial and command list for manager commands.",
    )
    async def cmd_helpless_manager(interaction: discord.Interaction) -> None:
        if not await require_music_manager(interaction, action="access manager help"):
            return
        try:
            await interaction.user.send(embed=_help_manager_tutorial_embed())
        except discord.Forbidden:
            # even more condescending since they are a manager but still "helpless" :P
            await interaction.response.send_message(
                "❌ How can someone trusted with managing the music library be so stupid "
                "that they use a command that will DM them a tutorial, but keeps their DMs closed?"
                " Don't you dare use this command again until you fix those settings!",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "How did you even get a manager role if you are this helpless? "
            "Look, I've sent you the tutorial, but that's all the help you're getting.",
            ephemeral=True,
        )


async def _send_toggle_song_result(
    interaction: discord.Interaction,
    song: dict,
    *,
    song_id: int,
) -> None:
    """Toggle a song's availability and send the appropriate response."""
    if song.get("available", 1):
        deactivate_song(song_id)
        await interaction.response.send_message(
            f"⛔ Deactivated **{song['name']}**.",
            ephemeral=True,
        )
        return
    activate_song(song_id)
    await interaction.response.send_message(
        f"✅ Re-activated **{song['name']}**.",
        ephemeral=True,
    )


async def _send_upload_followup(
    interaction: discord.Interaction,
    request: _UploadRequest,
) -> None:
    """Run upload processing and send the standard deferred follow-up."""
    if not await _validate_upload_request(interaction, request):
        return
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(
        await _handle_upload_song(interaction, request),
        ephemeral=True,
    )


def _media_list_embed(
    rows: list[str],
    header: str,
    title: str,
    colour: discord.Colour,
    suffix: str,
) -> discord.Embed:
    """Build a trimmed code-block embed for media list commands."""
    divider = "─" * len(header)
    shown_rows, hidden_count = _fit_rows_to_embed(header, divider, rows)
    lines = [header, divider, *shown_rows]
    if hidden_count:
        lines.append(f"... ({hidden_count} more {suffix} not shown)")
    embed = discord.Embed(title=title, colour=colour)
    embed.description = "```\n" + "\n".join(lines) + "\n```"
    embed.set_footer(text=f"{len(rows)} {suffix} total")
    return embed


def _register_upload_commands(bot: MusicBot) -> None:
    """Register upload commands for songs, ads, and broadcasts."""

    @bot.tree.command(
        name="upload_song",
        description="Add song media from attachment, YouTube link(s), or both.",
    )
    @app_commands.describe(
        source="Optional text with YouTube link(s)",
        file="Optional audio file attachment",
        name="Optional display name for attached file song",
        artist="Optional artist for attached file song",
    )
    async def cmd_upload_song(
        interaction: discord.Interaction,
        source: str | None = None,
        file: discord.Attachment | None = None,
        name: str | None = None,
        artist: str | None = None,
    ) -> None:
        request = _UploadRequest(source, file, name, artist, "song")
        await _send_upload_followup(interaction, request)

    @bot.tree.command(
        name="upload_ad",
        description="Add ad media from attachment, YouTube link(s), or both.",
    )
    @app_commands.describe(
        source="Optional text with YouTube link(s)",
        file="Optional audio file attachment",
        name="Optional ad name",
        sponsor="Optional sponsor name",
    )
    async def cmd_upload_ad(
        interaction: discord.Interaction,
        source: str | None = None,
        file: discord.Attachment | None = None,
        name: str | None = None,
        sponsor: str | None = None,
    ) -> None:
        request = _UploadRequest(source, file, name, sponsor, "ad")
        await _send_upload_followup(interaction, request)

    @bot.tree.command(
        name="upload_broadcast",
        description="Add DJ broadcasts from attachment or YouTube link(s).",
    )
    @app_commands.describe(
        source="Optional text with YouTube link(s)",
        file="Optional audio file attachment",
        sponsor="Optional sponsor name",
        placement="Optional `day` or `day:slot` (day=Mon|Tue|Wed|Thu|Fri|Sat|Sun;"
        "slot=intro|outro|event; default=today's day:event)",
    )
    async def cmd_upload_broadcast(
        interaction: discord.Interaction,
        source: str | None = None,
        file: discord.Attachment | None = None,
        sponsor: str | None = None,
        placement: str | None = None,
    ) -> None:
        day, slot = _parse_broadcast_placement(placement)
        request = _UploadRequest(
            source,
            file,
            None,
            sponsor,
            "broadcast",
            day=day,
            slot=slot or "event",
        )
        await _send_upload_followup(interaction, request)


def _register_search_and_playlist_commands(bot: MusicBot) -> None:
    """Register song search and playlist commands."""

    @bot.tree.command(
        name="search", description="Search songs by name, artist, uploader, or id."
    )
    @app_commands.describe(field="Field", query="Search term")
    @app_commands.choices(
        field=[
            app_commands.Choice(name="Name", value="name"),
            app_commands.Choice(name="Artist", value="artist"),
            app_commands.Choice(name="Added By (user ID)", value="added_by"),
            app_commands.Choice(name="ID", value="id"),
        ]
    )
    async def cmd_search(
        interaction: discord.Interaction,
        field: app_commands.Choice[str],
        query: str,
    ) -> None:
        results = search_songs(field.value, query)
        if not results:
            await interaction.response.send_message(
                "No matching songs found.",
                ephemeral=True,
            )
            return
        result_view = SongListView(results)
        result_view.update_buttons()
        embed = result_view.build_embed()
        embed.title = f'🔎 Results: {field.name} = "{query}"'
        await interaction.response.send_message(
            embed=embed,
            view=result_view,
            ephemeral=True,
        )

    @bot.tree.command(name="playlist", description="Show the playlist collection.")
    async def cmd_playlist(interaction: discord.Interaction) -> None:
        embed = await _song_table_embed(
            get_all_songs(),
            client=interaction.client,
            guild=interaction.guild,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="playlist_all",
        description=(
            "Show playlist including deactivated songs "
            "(Music Manager only)."
        ),
    )
    async def cmd_playlist_all(interaction: discord.Interaction) -> None:
        if not await require_music_manager(interaction, action="view the full playlist"):
            return
        embed = await _song_table_embed(
            get_all_songs_admin(),
            title="🎵 Song Library (All)",
            client=interaction.client,
            guild=interaction.guild,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _register_song_management_commands(bot: MusicBot) -> None:
    """Register manager song toggle/delete commands."""

    @bot.tree.command(
        name="toggle_song",
        description="Activate/deactivate a song by name.",
    )
    @app_commands.describe(name="Song name")
    async def cmd_toggle_song(interaction: discord.Interaction, name: str) -> None:
        if not await require_music_manager(interaction, action="toggle songs"):
            return
        matches = get_songs_by_name(name)
        if not matches:
            await interaction.response.send_message(
                f"Sorry, there is no song named **{name}**.",
                ephemeral=True,
            )
            return
        if len(matches) > 1:
            embed = await _song_table_embed(
                matches,
                title=f'🔎 Multiple songs named "{name}"',
                client=interaction.client,
                guild=interaction.guild,
            )
            embed.add_field(
                name="What to do",
                value="Use **`/toggle_song_id <id>`** with the ID shown above.",
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await _send_toggle_song_result(
            interaction,
            matches[0],
            song_id=matches[0]["id"],
        )

    @bot.tree.command(
        name="toggle_song_id",
        description="Activate/deactivate by song id.",
    )
    @app_commands.describe(song_id="Song ID")
    async def cmd_toggle_song_id(
        interaction: discord.Interaction,
        song_id: int,
    ) -> None:
        if not await require_music_manager(interaction, action="toggle songs"):
            return
        if (song := await _get_song_or_respond_missing(interaction, song_id)) is None:
            return
        await _send_toggle_song_result(interaction, song, song_id=song_id)

    @bot.tree.command(
        name="delete_song_id",
        description="Disable/delete a song by id with confirmation.",
    )
    @app_commands.describe(song_id="Song ID")
    async def cmd_delete_song_id(
        interaction: discord.Interaction,
        song_id: int,
    ) -> None:
        if not await require_music_manager(interaction, action="delete songs"):
            return
        if (song := await _get_song_or_respond_missing(interaction, song_id)) is None:
            return
        message_content = await _build_delete_confirm_message(
            song,
            interaction.user.id,
            interaction.client,
            interaction.guild,
        )
        await interaction.response.send_message(message_content)
        message = await interaction.original_response()
        await message.add_reaction(REACT_DEACTIVATE)
        await message.add_reaction(REACT_HARD_DELETE)
        await message.add_reaction(REACT_CANCEL)
        bot.pending_deletes[message.id] = {"user_id": interaction.user.id, "song": song}


def _register_media_list_commands(bot: MusicBot) -> None:
    """Register manager list commands for ads and broadcasts."""

    @bot.tree.command(name="ad_list", description="Show all ads (Music Manager only).")
    async def cmd_ad_list(interaction: discord.Interaction) -> None:
        if not await require_music_manager(interaction, action="view ads"):
            return
        ads = get_all_ads_admin()
        if not ads:
            await interaction.response.send_message("*No ads in the library yet.*", ephemeral=True)
            return
        header = f"{'ID':<4} {'Name':<20} {'Sponsor':<20} {'Plays':<5} {'Avail':<5}"
        rows = [
            f"{ad['id']:<4} {ad['name'][:20]:<20} {ad['sponsor'][:20]:<20} "
            f"{ad['times_played']:<5} {ad.get('available', 1)}"
            for ad in ads
        ]
        embed = _media_list_embed(rows, header, "📢 Ad Library", discord.Colour.orange(), "ad(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="broadcast_list",
        description="Show all broadcasts (Music Manager only).",
    )
    async def cmd_broadcast_list(interaction: discord.Interaction) -> None:
        if not await require_music_manager(interaction, action="view broadcasts"):
            return
        broadcasts = get_all_broadcasts_admin()
        if not broadcasts:
            await interaction.response.send_message(
                "*No broadcasts in the library yet.*",
                ephemeral=True,
            )
            return
        header = (
            f"{'ID':<4} {'Day':<10} {'Slot':<7} "
            f"{'Name':<18} {'Sponsor':<18} {'Plays':<5}"
        )
        rows = [
            f"{clip['id']:<4} {clip['day'][:10]:<10} {clip['slot'][:7]:<7} "
            f"{clip['name'][:18]:<18} {clip['sponsor'][:18]:<18} "
            f"{clip['times_played']:<5}"
            for clip in broadcasts
        ]
        embed = _media_list_embed(
            rows,
            header,
            "🎙️ Broadcast Library",
            discord.Colour.dark_teal(),
            "broadcast(s)",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _register_library_commands(bot: MusicBot) -> None:
    """Register song, ad, and broadcast library commands."""
    _register_upload_commands(bot)
    _register_search_and_playlist_commands(bot)
    _register_song_management_commands(bot)
    _register_media_list_commands(bot)


async def _send_dj_event_message(
    interaction: discord.Interaction,
    started: bool,
) -> None:
    """Send the outcome of a manual DJ event trigger."""
    if started:
        await interaction.response.send_message(
            "🎙️ Playing DJ event now.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        "❌ No DJ event clip available.",
        ephemeral=True,
    )


def _register_playback_commands(bot: MusicBot) -> None:
    """Register playback, feedback, and rigged-pool commands."""

    @bot.tree.command(
        name="set_rigged_pool",
        description="Set rigged song IDs as comma-separated list (empty/0 to clear).",
    )
    @app_commands.describe(song_ids="Example: 3,7,12")
    async def cmd_set_rigged_pool(
        interaction: discord.Interaction, song_ids: str
    ) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role.",
                ephemeral=True,
            )
            return
        ids: list[int] = []
        for part in [piece.strip() for piece in song_ids.split(",")]:
            if not part or part == "0":
                continue
            try:
                ids.append(int(part))
            except ValueError:
                continue
        bot.player.set_rigged_songs(ids)
        if not ids:
            await interaction.response.send_message(
                "Looks like the devil misplaced his playlist, it won't be lost forever though.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"{', '.join(str(song_id) for song_id in ids)} "
            "are Hell's greatest hits now. :smiling_imp:",
            ephemeral=True,
        )

    @bot.tree.command(name="play_dj_event", description="Play a DJ event clip now.")
    @app_commands.describe(
        day="Optional day abbreviation: MON TUE WED THU FRI SAT SUN",
        event_id="Optional broadcast ID from /broadcast_list to play directly",
    )
    async def cmd_play_dj_event(
        interaction: discord.Interaction,
        day: str | None = None,
        event_id: app_commands.Range[int, 1, 2147483647] | None = None,
    ) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to trigger DJ events.",
                ephemeral=True,
            )
            return
        if not bot.player.is_connected():
            await interaction.response.send_message(
                "❌ Bot is not connected to voice. Press Play/Resume first.",
                ephemeral=True,
            )
            return

        target_day = None
        if day:
            day_map = {
                "MON": "monday",
                "TUE": "tuesday",
                "WED": "wednesday",
                "THU": "thursday",
                "FRI": "friday",
                "SAT": "saturday",
                "SUN": "sunday",
            }
            target_day = day_map.get(day.upper())
            if target_day is None:
                await interaction.response.send_message(
                    "❌ Invalid day. Use MON TUE WED THU FRI SAT SUN.",
                    ephemeral=True,
                )
                return
        await _send_dj_event_message(
            interaction,
            await bot.player.play_dj_event_now(day=target_day, event_id=event_id),
        )

    @bot.tree.command(name="like", description="Send a like for the current song.")
    async def cmd_like(interaction: discord.Interaction) -> None:
        ok, message = await bot.submit_current_song_feedback(interaction, is_like=True)
        prefix = "👍" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {message}", ephemeral=True)

    @bot.tree.command(
        name="dislike", description="Send a dislike for the current song."
    )
    async def cmd_dislike(interaction: discord.Interaction) -> None:
        ok, message = await bot.submit_current_song_feedback(interaction, is_like=False)
        prefix = "👎" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {message}", ephemeral=True)


async def _handle_persona_switch(
    interaction: discord.Interaction,
    bot: MusicBot,
    persona_mode: str,
) -> None:
    """Switch persona modes and report the resulting state."""
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role.",
            ephemeral=True,
        )
        return
    switched = await bot.switch_persona_with_branding(persona_mode)
    if not switched:
        await interaction.response.send_message(
            "⚠️ The veil would not shift. Something old and patient is resisting the "
            "change.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(await bot.state_announcement())


async def _start_purge_confirmation(
    interaction: discord.Interaction,
    bot: MusicBot,
) -> None:
    """Create and display the one-time password used for the purge modal."""
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role.",
            ephemeral=True,
        )
        return

    alphabet = string.ascii_letters + string.digits
    confirmation_code = "".join(secrets.choice(alphabet) for _ in range(10))
    bot.purge_code = confirmation_code

    print("\n" + "=" * 60, flush=True)
    print(
        f"[PURGE CONFIRM] One-time password: {confirmation_code}", flush=True
    )  # noqa: S106
    print("=" * 60 + "\n", flush=True)

    await interaction.response.send_modal(PurgeSongsConfirmModal())


def _register_persona_commands(bot: MusicBot) -> None:
    """Register persona switching and purge commands."""

    @bot.tree.command(
        name="pardon",
        description="Collector is returned to the land of the living, for now.",
    )
    async def cmd_pardon(interaction: discord.Interaction) -> None:
        await _handle_persona_switch(interaction, bot, "collector")

    @bot.tree.command(
        name="damn",
        description="Get access to SufferingFML at the low cost of damning the Collector to hell.",
    )
    async def cmd_damn(interaction: discord.Interaction) -> None:
        await _handle_persona_switch(interaction, bot, "suffering")

    @bot.tree.command(
        name="save",
        description="Get access to HeavenIN by saving the Collector from eternal suffering.",
    )
    async def cmd_save(interaction: discord.Interaction) -> None:
        await _handle_persona_switch(interaction, bot, "heaven")

    @bot.tree.command(
        name="purge_songs",
        description=(
            "[DANGER] Wipe ALL songs from the database and disk "
            "(requires terminal password)."
        ),
    )
    async def cmd_purge_songs(interaction: discord.Interaction) -> None:
        await _start_purge_confirmation(interaction, bot)


def register_commands(bot: MusicBot) -> None:
    """Register all slash commands for the music bot."""
    _register_controller_commands(bot)
    _register_library_commands(bot)
    _register_playback_commands(bot)
    _register_persona_commands(bot)
