from __future__ import annotations

import asyncio
import logging
import secrets
import string
import time

import discord
from discord import app_commands

from app_bot import MusicBot, _help_tutorial_embed, _infer_target, _unique_path
from config import ADS_DIR, ALLOWED_EXTENSIONS, SONGS_DIR
from database import (
    activate_song,
    add_song,
    deactivate_song,
    get_all_songs,
    get_all_songs_admin,
    get_song,
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
    _song_table_embed,
    is_music_manager,
)

log = logging.getLogger(__name__)


class PurgeSongsConfirmModal(discord.ui.Modal, title="Confirm Full Song Purge"):
    password = discord.ui.TextInput(
        label="Terminal confirmation password",
        placeholder="Check the bot terminal output for the password",
        max_length=20,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        client = interaction.client  # type: ignore[attr-defined]
        if not client._purge_code or time.time() > client._purge_code_expiry:
            await interaction.response.send_message(
                "❌ The purge session has expired. Run `/purge_songs` again.",
                ephemeral=True,
            )
            return
        if self.password.value.strip() != client._purge_code:
            await interaction.response.send_message(
                "❌ Incorrect password. Purge aborted.",
                ephemeral=True,
            )
            return

        client._purge_code = None
        client._purge_code_expiry = 0.0

        await interaction.response.defer(ephemeral=True)
        deleted = purge_all_songs()
        deleted_files = 0
        for p in SONGS_DIR.iterdir():
            if p.is_file() and p.name != ".gitkeep":
                try:
                    p.unlink()
                    deleted_files += 1
                except Exception as exc:
                    log.warning("Could not delete file %s during purge: %s", p, exc)

        log.warning(
            "PURGE executed by user %s: %d DB records deleted, %d files removed.",
            interaction.user.id, len(deleted), deleted_files,
        )
        await interaction.followup.send(
            f"🗑️ Purged **{len(deleted)}** song(s) from the database and removed **{deleted_files}** file(s).",
            ephemeral=True,
        )


def register_commands(bot: MusicBot) -> None:
    @bot.tree.command(name="controller", description="Post the controller panel.")
    async def cmd_controller(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=bot._active_controller_embed(),
            view=bot._active_controller_view(),
        )

    @bot.tree.command(name="help", description="DM a quick tutorial and command list.")
    async def cmd_help(interaction: discord.Interaction) -> None:
        try:
            await interaction.user.send(embed=_help_tutorial_embed())
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I couldn't DM you. Please enable direct messages from server members and try again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "📬 I sent you a quick tutorial and command list in DMs.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="upload_song",
        description="Add media from attachment, YouTube link(s), or both.",
    )
    @app_commands.describe(
        source="Optional text with YouTube link(s). You can include kind:song or kind:ad.",
        file="Optional audio file attachment",
        target="Destination type (Song or Ad). Defaults to Song.",
        name="Optional display name override for attached file song",
        artist="Optional artist override for attached file song",
    )
    @app_commands.choices(
        target=[
            app_commands.Choice(name="Song", value="song"),
            app_commands.Choice(name="Ad", value="ad"),
        ]
    )
    async def cmd_upload_song(
        interaction: discord.Interaction,
        source: str | None = None,
        file: discord.Attachment | None = None,
        target: app_commands.Choice[str] | None = None,
        name: str | None = None,
        artist: str | None = None,
    ) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to upload songs.", ephemeral=True
            )
            return

        raw_target = _infer_target(source, target.value if target else None)
        target_dir = SONGS_DIR if raw_target == "song" else ADS_DIR
        urls = extract_urls(source or "")
        youtube_urls = [u for u in urls if is_youtube_url(u)]

        if file is None and not youtube_urls:
            await interaction.response.send_message(
                "❌ Provide an attachment, a YouTube link, or both.",
                ephemeral=True,
            )
            return

        if file is not None:
            ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
            if ext not in ALLOWED_EXTENSIONS:
                await interaction.response.send_message(
                    f"❌ Unsupported file type `{ext}`.\nAllowed: {', '.join(ALLOWED_EXTENSIONS)}",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True)

        added_song_ids: list[int] = []
        added_ad_files: list[str] = []
        ignored_urls = [u for u in urls if u not in youtube_urls]
        failed_urls: list[str] = []

        for url in youtube_urls:
            try:
                downloaded, playlist_title = await asyncio.to_thread(
                    download_youtube_audio, url, target_dir, False
                )
            except Exception as exc:
                log.warning("yt-dlp download failed for %s: %s", url, exc)
                failed_urls.append(url)
                continue
            for p in downloaded:
                if raw_target == "song":
                    dl_artist = playlist_title or "YouTube"
                    sid = add_song(p.stem, dl_artist, p.name, str(interaction.user.id))
                    added_song_ids.append(sid)
                else:
                    added_ad_files.append(p.name)

        if file is not None:
            dest = _unique_path(target_dir, file.filename)
            await file.save(dest)
            if raw_target == "song":
                display_name = (name or dest.stem).strip() or dest.stem
                display_artist = (artist or "Unknown").strip() or "Unknown"
                sid = add_song(display_name, display_artist, dest.name, str(interaction.user.id))
                added_song_ids.append(sid)
            else:
                added_ad_files.append(dest.name)

        if raw_target == "ad" and (added_ad_files or youtube_urls):
            sync_ads_from_disk()

        lines: list[str] = []
        if added_song_ids:
            lines.append(f"✅ Added {len(added_song_ids)} song(s). IDs: `{', '.join(map(str, added_song_ids))}`")
        if added_ad_files:
            lines.append(f"✅ Added {len(added_ad_files)} ad file(s).")
        if ignored_urls:
            lines.append(f"⚠️ Ignored {len(ignored_urls)} non-YouTube URL(s).")
        if failed_urls:
            lines.append(f"⚠️ Failed to download {len(failed_urls)} YouTube URL(s).")
        if not lines:
            lines.append("❌ No media was added.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @bot.tree.command(name="search", description="Search songs by name, artist, uploader, or id.")
    @app_commands.describe(field="Field", query="Search term")
    @app_commands.choices(field=[
        app_commands.Choice(name="Name", value="name"),
        app_commands.Choice(name="Artist", value="artist"),
        app_commands.Choice(name="Added By (user ID)", value="added_by"),
        app_commands.Choice(name="ID", value="id"),
    ])
    async def cmd_search(
        interaction: discord.Interaction,
        field: app_commands.Choice[str],
        query: str,
    ) -> None:
        results = search_songs(field.value, query)
        if not results:
            await interaction.response.send_message("No matching songs found.", ephemeral=True)
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
                "❌ You need the **Music Manager** role to toggle songs.", ephemeral=True
            )
            return
        matches = get_songs_by_name(name)
        if not matches:
            await interaction.response.send_message(
                f"Sorry, there is no song named **{name}**.", ephemeral=True
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
        song = matches[0]
        if song.get("available", 1):
            deactivate_song(song["id"])
            await interaction.response.send_message(f"⛔ Deactivated **{song['name']}**.", ephemeral=True)
        else:
            activate_song(song["id"])
            await interaction.response.send_message(f"✅ Re-activated **{song['name']}**.", ephemeral=True)

    @bot.tree.command(name="toggle_song_id", description="Activate/deactivate by song id.")
    @app_commands.describe(song_id="Song ID")
    async def cmd_toggle_song_id(interaction: discord.Interaction, song_id: int) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to toggle songs.", ephemeral=True
            )
            return
        song = get_song(song_id)
        if not song:
            await interaction.response.send_message(
                f"Sorry, there is no song with ID **{song_id}**.", ephemeral=True
            )
            return
        if song.get("available", 1):
            deactivate_song(song_id)
            await interaction.response.send_message(f"⛔ Deactivated **{song['name']}**.", ephemeral=True)
        else:
            activate_song(song_id)
            await interaction.response.send_message(f"✅ Re-activated **{song['name']}**.", ephemeral=True)

    @bot.tree.command(name="delete_song_id", description="Begin two-step delete by song id.")
    @app_commands.describe(song_id="Song ID")
    async def cmd_delete_song_id(interaction: discord.Interaction, song_id: int) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to delete songs.", ephemeral=True
            )
            return
        song = get_song(song_id)
        if not song:
            await interaction.response.send_message(
                f"Sorry, there is no song with ID **{song_id}**.", ephemeral=True
            )
            return
        msg_content = await _build_delete_confirm_message(
            song, interaction.user.id, interaction.client, interaction.guild
        )
        await interaction.response.send_message(msg_content)
        msg = await interaction.original_response()
        await msg.add_reaction(REACT_DEACTIVATE)
        await msg.add_reaction(REACT_HARD_DELETE)
        bot.pending_deletes[msg.id] = {"user_id": interaction.user.id, "song": song}

    @bot.tree.command(name="songs", description="Show active songs.")
    async def cmd_songs(interaction: discord.Interaction) -> None:
        embed = await _song_table_embed(get_all_songs(), compact=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="songs_all", description="Show all songs including deactivated (Music Manager only).")
    async def cmd_songs_all(interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to view the full library.", ephemeral=True
            )
            return
        embed = await _song_table_embed(
            get_all_songs_admin(),
            title="🎵 Song Library (All)",
            client=interaction.client,
            guild=interaction.guild,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(
        name="set_rigged_pool",
        description="Set rigged song IDs as comma-separated list (empty/0 to clear).",
    )
    @app_commands.describe(song_ids="Example: 3,7,12")
    async def cmd_set_rigged_pool(interaction: discord.Interaction, song_ids: str) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role.", ephemeral=True
            )
            return
        raw = [p.strip() for p in song_ids.split(",")]
        ids: list[int] = []
        for p in raw:
            if not p or p == "0":
                continue
            try:
                ids.append(int(p))
            except ValueError:
                continue
        bot.player.set_rigged_songs(ids)
        if not ids:
            await interaction.response.send_message("🎭 Rigged song pool cleared.", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"🎭 Rigged song pool set to IDs: {', '.join(str(i) for i in ids)}",
                ephemeral=True,
            )

    @bot.tree.command(name="now_playing", description="Show current song.")
    async def cmd_now_playing(interaction: discord.Interaction) -> None:
        song = bot.player.current_song
        if not song:
            await interaction.response.send_message("❌ Nothing is playing right now.", ephemeral=True)
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
                "❌ You need the **Music Manager** role to trigger DJ events.", ephemeral=True
            )
            return
        if not bot.player.is_connected():
            await interaction.response.send_message(
                "❌ Bot is not connected to voice. Press Play/Resume first.", ephemeral=True
            )
            return
        target_day = None
        if day:
            day_map = {
                "MON": "monday", "TUE": "tuesday", "WED": "wednesday",
                "THU": "thursday", "FRI": "friday", "SAT": "saturday", "SUN": "sunday",
            }
            target_day = day_map.get(day.upper())
            if target_day is None:
                await interaction.response.send_message(
                    "❌ Invalid day. Use MON TUE WED THU FRI SAT SUN.", ephemeral=True
                )
                return
        started = await bot.player.play_dj_event_now(day=target_day)
        if started:
            await interaction.response.send_message("🎙️ Playing DJ event now.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ No DJ event clip available.", ephemeral=True)

    @bot.tree.command(name="like", description="Send a like for the current song.")
    async def cmd_like(interaction: discord.Interaction) -> None:
        ok, msg = await bot.submit_current_song_feedback(interaction, is_like=True)
        prefix = "👍" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)

    @bot.tree.command(name="dislike", description="Send a dislike for the current song.")
    async def cmd_dislike(interaction: discord.Interaction) -> None:
        ok, msg = await bot.submit_current_song_feedback(interaction, is_like=False)
        prefix = "👎" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)

    @bot.tree.command(name="pardon", description="Switch to the limited shady controller mode.")
    async def cmd_pardon(interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role.", ephemeral=True
            )
            return
        switched = await bot._switch_persona_with_branding("collector")
        if not switched:
            await interaction.response.send_message(
                "⚠️ The veil would not shift. Something old and patient is resisting the change.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(await bot._state_announcement())

    @bot.tree.command(name="damn", description="Switch to full mode with default day-based persona.")
    async def cmd_damn(interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role.", ephemeral=True
            )
            return
        switched = await bot._switch_persona_with_branding("day_cycle")
        if not switched:
            await interaction.response.send_message(
                "⚠️ The veil would not shift. Something old and patient is resisting the change.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(await bot._state_announcement())

    @bot.tree.command(name="save", description="Switch to full mode and force Heaven persona.")
    async def cmd_save(interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role.", ephemeral=True
            )
            return
        switched = await bot._switch_persona_with_branding("forced_heaven")
        if not switched:
            await interaction.response.send_message(
                "⚠️ The veil would not shift. Something old and patient is resisting the change.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(await bot._state_announcement())

    @bot.tree.command(
        name="purge_songs",
        description="[DANGER] Wipe ALL songs from the database and disk (requires terminal password).",
    )
    async def cmd_purge_songs(interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role.", ephemeral=True
            )
            return

        alphabet = string.ascii_letters + string.digits
        confirmation_code = "".join(secrets.choice(alphabet) for _ in range(10))
        bot._purge_code = confirmation_code
        bot._purge_code_expiry = time.time() + 300

        print("\n" + "=" * 60, flush=True)
        print(f"[PURGE CONFIRM] One-time password: {confirmation_code}", flush=True)  # noqa: S106
        print("[PURGE CONFIRM] Password expires in 5 minutes.", flush=True)
        print("=" * 60 + "\n", flush=True)

        await interaction.response.send_modal(PurgeSongsConfirmModal())
