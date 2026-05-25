"""Slash command registration for playback, library management, and persona controls."""
from __future__ import annotations

import asyncio
import logging
import secrets
import string
import time
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from yt_dlp.utils import DownloadError

from app_bot import MusicBot, _help_tutorial_embed, _infer_target, _unique_path
from config import ADS_DIR, ALLOWED_EXTENSIONS, SONGS_DIR
from database import (
    activate_song,
    add_song,
    deactivate_song,
    get_all_songs,
    get_all_songs_admin,
    get_songs_by_name,
    purge_all_songs,
    search_songs,
    sync_ads_from_disk,
)
from media_utils import download_youtube_audio, extract_urls, is_youtube_url
from views import (
    REACT_DEACTIVATE,
    REACT_HARD_DELETE,
    _build_delete_confirm_message,
    _get_song_or_respond_missing,
    _song_table_embed,
    is_music_manager,
    require_music_manager,
)

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _UploadRequest:
    """Normalized upload request assembled from slash-command options."""

    source: str | None
    file: discord.Attachment | None
    raw_target: str
    name: str | None
    artist: str | None


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
        if not client.purge_code or time.time() > client.purge_code_expiry:
            await interaction.response.send_message(
                "❌ The purge session has expired. Run `/purge_songs` again.",
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
        client.purge_code_expiry = 0.0
        await interaction.response.defer(ephemeral=True)

        deleted = purge_all_songs()
        deleted_files = 0
        for path in SONGS_DIR.iterdir():
            if not path.is_file() or path.name == ".gitkeep":
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


def _build_upload_request(
    source: str | None,
    file: discord.Attachment | None,
    name: str | None,
    artist: str | None,
) -> _UploadRequest:
    """Create a normalized upload request from slash-command arguments."""
    return _UploadRequest(
        source=source,
        file=file,
        raw_target=_infer_target(source, None),
        name=name,
        artist=artist,
    )


async def _validate_upload_request(
    interaction: discord.Interaction,
    request: _UploadRequest,
) -> bool:
    """Validate permissions and upload inputs before starting work."""
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to upload songs.",
            ephemeral=True,
        )
        return False

    youtube_urls = [url for url in extract_urls(request.source or "") if is_youtube_url(url)]
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


async def _store_downloaded_media(
    interaction: discord.Interaction,
    request: _UploadRequest,
    target_dir: Path,
) -> tuple[list[int], list[str], list[str], list[str]]:
    """Download and store media from YouTube URLs."""
    urls = extract_urls(request.source or "")
    youtube_urls = [url for url in urls if is_youtube_url(url)]
    added_song_ids: list[int] = []
    added_ad_files: list[str] = []
    failed_urls: list[str] = []

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
            if request.raw_target == "song":
                try:
                    song_id = add_song(
                        path.stem,
                        playlist_title or "YouTube",
                        path.name,
                        str(interaction.user.id),
                    )
                except ValueError:
                    failed_urls.append(url)
                    continue
                added_song_ids.append(song_id)
            else:
                added_ad_files.append(path.name)

    ignored_urls = [url for url in urls if url not in youtube_urls]
    return added_song_ids, added_ad_files, ignored_urls, failed_urls


async def _store_uploaded_attachment(
    interaction: discord.Interaction,
    request: _UploadRequest,
    target_dir: Path,
) -> tuple[list[int], list[str], str | None]:
    """Store an uploaded attachment in the song or ad library."""
    if request.file is None:
        return [], [], None

    destination = _unique_path(target_dir, request.file.filename)
    await request.file.save(destination)
    if request.raw_target == "ad":
        return [], [destination.name], None

    display_name = (request.name or destination.stem).strip() or destination.stem
    display_artist = (request.artist or "Unknown").strip() or "Unknown"
    try:
        song_id = add_song(
            display_name,
            display_artist,
            destination.name,
            str(interaction.user.id),
        )
    except ValueError:
        if destination.exists():
            destination.unlink()
        return [], [], "❌ Invalid reserved filename; upload was skipped."
    return [song_id], [], None


def _upload_summary(
    added_song_ids: list[int],
    added_ad_files: list[str],
    ignored_urls: list[str],
    failed_urls: list[str],
) -> str:
    """Build the final upload status message."""
    lines: list[str] = []
    if added_song_ids:
        lines.append(
            "✅ Added "
            f"{len(added_song_ids)} song(s). IDs: `{', '.join(map(str, added_song_ids))}`"
        )
    if added_ad_files:
        lines.append(f"✅ Added {len(added_ad_files)} ad file(s).")
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
    target_dir = SONGS_DIR if request.raw_target == "song" else ADS_DIR
    added_song_ids, added_ad_files, ignored_urls, failed_urls = await _store_downloaded_media(
        interaction,
        request,
        target_dir,
    )
    file_song_ids, file_ad_files, error_message = await _store_uploaded_attachment(
        interaction,
        request,
        target_dir,
    )
    if error_message is not None:
        return error_message

    added_song_ids.extend(file_song_ids)
    added_ad_files.extend(file_ad_files)
    if request.raw_target == "ad" and (added_ad_files or extract_urls(request.source or "")):
        sync_ads_from_disk()
    return _upload_summary(added_song_ids, added_ad_files, ignored_urls, failed_urls)


def _register_controller_commands(bot: MusicBot) -> None:
    """Register general controller and tutorial commands."""

    @bot.tree.command(name="controller", description="Post the controller panel.")
    async def cmd_controller(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=bot.active_controller_embed(),
            view=bot.active_controller_view(),
        )

    @bot.tree.command(name="help", description="DM a quick tutorial and command list.")
    async def cmd_help(interaction: discord.Interaction) -> None:
        try:
            await interaction.user.send(embed=_help_tutorial_embed())
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I couldn't DM you. Please enable direct messages from server members "
                "and try again.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "📬 I sent you a quick tutorial and command list in DMs.",
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


def _register_library_commands(bot: MusicBot) -> None:
    """Register song library management and search commands."""

    @bot.tree.command(
        name="upload_song",
        description="Add media from attachment, YouTube link(s), or both.",
    )
    @app_commands.describe(
        source=(
            "Optional text with YouTube link(s). You can also include kind:song or "
            "kind:ad."
        ),
        file="Optional audio file attachment",
        name="Optional display name override for attached file song",
        artist="Optional artist override for attached file song",
    )
    async def cmd_upload_song(
        interaction: discord.Interaction,
        source: str | None = None,
        file: discord.Attachment | None = None,
        name: str | None = None,
        artist: str | None = None,
    ) -> None:
        request = _build_upload_request(source, file, name, artist)
        if not await _validate_upload_request(interaction, request):
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            await _handle_upload_song(interaction, request),
            ephemeral=True,
        )

    @bot.tree.command(name="search", description="Search songs by name, artist, uploader, or id.")
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
        embed = await _song_table_embed(
            results,
            title=f'🔎 Results: {field.name} = "{query}"',
            client=interaction.client,
            guild=interaction.guild,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="toggle_song", description="Activate/deactivate a song by name.")
    @app_commands.describe(name="Song name")
    async def cmd_toggle_song(interaction: discord.Interaction, name: str) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to toggle songs.",
                ephemeral=True,
            )
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
        await _send_toggle_song_result(interaction, matches[0], song_id=matches[0]["id"])

    @bot.tree.command(name="toggle_song_id", description="Activate/deactivate by song id.")
    @app_commands.describe(song_id="Song ID")
    async def cmd_toggle_song_id(interaction: discord.Interaction, song_id: int) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to toggle songs.",
                ephemeral=True,
            )
            return
        if (song := await _get_song_or_respond_missing(interaction, song_id)) is None:
            return
        await _send_toggle_song_result(interaction, song, song_id=song_id)

    @bot.tree.command(name="delete_song_id", description="Begin two-step delete by song id.")
    @app_commands.describe(song_id="Song ID")
    async def cmd_delete_song_id(interaction: discord.Interaction, song_id: int) -> None:
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
        bot.pending_deletes[message.id] = {"user_id": interaction.user.id, "song": song}

    @bot.tree.command(name="songs", description="Show active songs.")
    async def cmd_songs(interaction: discord.Interaction) -> None:
        embed = await _song_table_embed(get_all_songs(), compact=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="songs_all",
        description="Show all songs including deactivated (Music Manager only).",
    )
    async def cmd_songs_all(interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to view the full library.",
                ephemeral=True,
            )
            return
        embed = await _song_table_embed(
            get_all_songs_admin(),
            title="🎵 Song Library (All)",
            client=interaction.client,
            guild=interaction.guild,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
    async def cmd_set_rigged_pool(interaction: discord.Interaction, song_ids: str) -> None:
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
                "🎭 Rigged song pool cleared.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"🎭 Rigged song pool set to IDs: {', '.join(str(song_id) for song_id in ids)}",
            ephemeral=True,
        )

    @bot.tree.command(name="now_playing", description="Show current song.")
    async def cmd_now_playing(interaction: discord.Interaction) -> None:
        song = bot.player.current_song
        if not song:
            await interaction.response.send_message(
                "❌ Nothing is playing right now.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(title="🎵 Now Playing", colour=discord.Colour.green())
        embed.add_field(name="Song", value=song["name"], inline=True)
        embed.add_field(name="Artist", value=song["artist"], inline=True)
        embed.add_field(name="Plays", value=str(song.get("times_played", 0)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="play_dj_event", description="Play a random DJ event clip now.")
    @app_commands.describe(day="Optional day abbreviation: MON TUE WED THU FRI SAT SUN")
    async def cmd_play_dj_event(
        interaction: discord.Interaction,
        day: str | None = None,
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
            await bot.player.play_dj_event_now(day=target_day),
        )

    @bot.tree.command(name="like", description="Send a like for the current song.")
    async def cmd_like(interaction: discord.Interaction) -> None:
        ok, message = await bot.submit_current_song_feedback(interaction, is_like=True)
        prefix = "👍" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {message}", ephemeral=True)

    @bot.tree.command(name="dislike", description="Send a dislike for the current song.")
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
    bot.purge_code_expiry = time.time() + 300

    print("\n" + "=" * 60, flush=True)
    print(f"[PURGE CONFIRM] One-time password: {confirmation_code}", flush=True)  # noqa: S106
    print("[PURGE CONFIRM] Password expires in 5 minutes.", flush=True)
    print("=" * 60 + "\n", flush=True)

    await interaction.response.send_modal(PurgeSongsConfirmModal())


def _register_persona_commands(bot: MusicBot) -> None:
    """Register persona switching and purge commands."""

    @bot.tree.command(name="pardon", description="Switch to the limited shady controller mode.")
    async def cmd_pardon(interaction: discord.Interaction) -> None:
        await _handle_persona_switch(interaction, bot, "collector")

    @bot.tree.command(
        name="damn",
        description="Switch to full mode with default day-based persona.",
    )
    async def cmd_damn(interaction: discord.Interaction) -> None:
        await _handle_persona_switch(interaction, bot, "day_cycle")

    @bot.tree.command(
        name="save",
        description="Switch to full mode and force Heaven persona.",
    )
    async def cmd_save(interaction: discord.Interaction) -> None:
        await _handle_persona_switch(interaction, bot, "forced_heaven")

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
