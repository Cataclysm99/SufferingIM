"""Modal components for song management."""

from __future__ import annotations

import asyncio

import discord

from config import ALLOWED_EXTENSIONS, SONGS_DIR
from database import (
    add_song,
    deactivate_song,
    get_disabled_song_filenames,
    get_song,
    get_songs_by_name,
    hard_delete_song,
)
from media_utils import download_youtube_audio, extract_urls, is_youtube_url

from .helpers import _resolve_username, _song_table_embed
from .permissions import is_music_manager


class AddSongModal(discord.ui.Modal, title="Add Song"):
    song_name = discord.ui.TextInput(
        label="Song Name (optional)",
        placeholder="Overrides title for 1st track only; leave blank for playlists",
        max_length=100,
        required=False,
    )
    artist = discord.ui.TextInput(
        label="Artist (optional)",
        placeholder="Defaults to playlist name for playlists, or 'YouTube'",
        max_length=100,
        required=False,
    )
    youtube_url = discord.ui.TextInput(
        label="YouTube Link (video or playlist)",
        placeholder="https://www.youtube.com/watch?v=... or playlist?list=...",
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        urls = extract_urls(self.youtube_url.value or "")
        if not urls:
            await interaction.response.send_message(
                "❌ Please provide a valid YouTube URL.",
                ephemeral=True,
            )
            return

        youtube_url = urls[0]
        if not is_youtube_url(youtube_url):
            await interaction.response.send_message(
                "❌ Only YouTube links are supported here.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            downloaded, playlist_title = await asyncio.to_thread(
                download_youtube_audio, youtube_url, SONGS_DIR, False
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Could not download from YouTube: {exc}", ephemeral=True)
            return

        if not downloaded:
            await interaction.followup.send(
                f"❌ Downloaded file type is not supported.\nAllowed: {', '.join(ALLOWED_EXTENSIONS)}",
                ephemeral=True,
            )
            return

        added_by = str(interaction.user.id)
        is_manager = is_music_manager(interaction)
        disabled_filenames = get_disabled_song_filenames()
        artist_input = (self.artist.value or "").strip()
        artist = artist_input or playlist_title or "YouTube"
        custom_name = (self.song_name.value or "").strip()
        lines: list[str] = []
        blocked_lines: list[str] = []

        for idx, track in enumerate(downloaded):
            if not is_manager and track.name in disabled_filenames:
                blocked_lines.append(
                    f"⛔ **{track.stem}** was previously disabled and can only be added by a **Music Manager**."
                )
                continue
            name = custom_name if idx == 0 and custom_name else track.stem
            try:
                song_id = add_song(name, artist, track.name, added_by)
            except ValueError:
                blocked_lines.append(f"⛔ Skipped invalid filename: `{track.name}`.")
                continue
            lines.append(f"✅ Added **{name}** (ID: `{song_id}`)")
        if not lines and blocked_lines:
            await interaction.followup.send("\n".join(blocked_lines), ephemeral=True)
            return
        if blocked_lines:
            lines.extend(blocked_lines)
        await interaction.followup.send("\n".join(lines), ephemeral=True)


class DeleteSongModal(discord.ui.Modal, title="Delete Song"):
    song_name = discord.ui.TextInput(
        label="Song Name",
        placeholder="e.g. Bohemian Rhapsody",
        max_length=100,
    )
    hard_delete = discord.ui.TextInput(
        label="Delete completely? (Y/N)",
        placeholder="N = disable only, Y = delete DB entry + file",
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to delete songs.", ephemeral=True
            )
            return

        query = self.song_name.value
        matches = get_songs_by_name(query)

        if not matches:
            await interaction.response.send_message(
                f"Sorry, there is no song named **{query}**.",
                ephemeral=True,
            )
            return

        raw_choice = (self.hard_delete.value or "").strip().lower()
        if raw_choice not in {"y", "yes", "n", "no"}:
            await interaction.response.send_message(
                "❌ Enter **Y** to fully delete the song or **N** to disable it.",
                ephemeral=True,
            )
            return
        should_hard_delete = raw_choice in {"y", "yes"}

        if len(matches) > 1:
            usernames = await asyncio.gather(
                *[_resolve_username(interaction.client, m.get("added_by", ""), interaction.guild) for m in matches]
            )

            lines = []
            for song, username in zip(matches, usernames):
                status = "✅" if song.get("available", 1) else "⛔"
                lines.append(
                    f"{status} ID `{song['id']}` — **{song['name']}** "
                    f"by **{song['artist']}** — added by **{username}**"
                )

            embed = discord.Embed(
                title=f'🔎 Multiple songs named "{query}"',
                description="\n".join(lines),
                colour=discord.Colour.orange(),
            )
            embed.add_field(
                name="What to do",
                value=(
                    "Identify the song you want to remove from the list above, then use:\n"
                    "**`/delete_song_id <id>`** — if you want to target one exact match."
                ),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        song = matches[0]
        if should_hard_delete:
            hard_delete_song(song["id"])
            song_path = SONGS_DIR / song["filename"]
            if song_path.exists():
                song_path.unlink()
            await interaction.response.send_message(
                f"🗑️ Permanently deleted **{song['name']}** by **{song['artist']}**.",
                ephemeral=True,
            )
            return

        if not song.get("available", 1):
            await interaction.response.send_message(
                f"⛔ **{song['name']}** is already disabled.",
                ephemeral=True,
            )
            return

        deactivate_song(song["id"])
        await interaction.response.send_message(
            (
                f"⛔ Disabled **{song['name']}** by **{song['artist']}**.\n"
                "Use `/toggle_song` or `/toggle_song_id` to re-enable it."
            ),
            ephemeral=True,
        )


class DisableSongModal(discord.ui.Modal, title="Disable Song"):
    song_name = discord.ui.TextInput(
        label="Song Name",
        placeholder="e.g. Bohemian Rhapsody",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to disable songs.", ephemeral=True
            )
            return

        query = self.song_name.value
        matches = get_songs_by_name(query)
        if not matches:
            await interaction.response.send_message(
                f"Sorry, there is no song named **{query}**.",
                ephemeral=True,
            )
            return

        if len(matches) > 1:
            embed = await _song_table_embed(
                matches,
                title=f'🔎 Multiple songs named "{query}"',
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
        if not song.get("available", 1):
            await interaction.response.send_message(
                f"⛔ **{song['name']}** is already disabled.",
                ephemeral=True,
            )
            return

        deactivate_song(song["id"])
        await interaction.response.send_message(
            f"⛔ Disabled **{song['name']}** by **{song['artist']}** (ID: `{song['id']}`).",
            ephemeral=True,
        )


class DeleteSongByIdModal(discord.ui.Modal, title="Delete / Disable Song by ID"):
    song_id = discord.ui.TextInput(
        label="Song ID",
        placeholder="Enter the numeric ID shown in Playlist view",
        max_length=10,
    )
    hard_delete = discord.ui.TextInput(
        label="Delete completely? (Y/N)",
        placeholder="N = disable only, Y = remove DB entry + file",
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to delete songs.", ephemeral=True
            )
            return

        try:
            sid = int(self.song_id.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Song ID must be a number.", ephemeral=True
            )
            return

        song = get_song(sid)
        if not song:
            await interaction.response.send_message(
                f"Sorry, there is no song with ID **{sid}**.", ephemeral=True
            )
            return

        raw_choice = (self.hard_delete.value or "").strip().lower()
        if raw_choice not in {"y", "yes", "n", "no"}:
            await interaction.response.send_message(
                "❌ Enter **Y** to fully delete the song or **N** to disable it.",
                ephemeral=True,
            )
            return
        should_hard_delete = raw_choice in {"y", "yes"}

        if should_hard_delete:
            hard_delete_song(song["id"])
            song_path = SONGS_DIR / song["filename"]
            if song_path.exists():
                song_path.unlink()
            await interaction.response.send_message(
                f"🗑️ Permanently deleted **{song['name']}** (ID: `{song['id']}`) by **{song['artist']}**.",
                ephemeral=True,
            )
            return

        if not song.get("available", 1):
            await interaction.response.send_message(
                f"⛔ **{song['name']}** (ID: `{song['id']}`) is already disabled.",
                ephemeral=True,
            )
            return

        deactivate_song(song["id"])
        await interaction.response.send_message(
            (
                f"⛔ Disabled **{song['name']}** (ID: `{song['id']}`) by **{song['artist']}**.\n"
                "Use `/toggle_song_id` to re-enable it."
            ),
            ephemeral=True,
        )

