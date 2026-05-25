"""Modal components for song management."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import discord
from yt_dlp.utils import DownloadError

from config import ALLOWED_EXTENSIONS, SONGS_DIR
from database import (
    add_song,
    deactivate_song,
    get_disabled_song_filenames,
    get_songs_by_name,
    hard_delete_song,
)
from media_utils import download_youtube_audio, extract_urls, is_youtube_url

from .helpers import _get_song_or_respond_missing, _resolve_username
from .permissions import is_music_manager, require_music_manager


@dataclass(slots=True)
class _DownloadSummaryContext:
    """Shared context used while storing downloaded tracks."""

    added_by: str
    is_manager: bool
    disabled_filenames: set[str]
    artist: str


class AddSongModal(discord.ui.Modal, title="Add Song"):
    """Collect a YouTube link and optional metadata for new songs."""

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

    def first_youtube_url(self) -> str | None:
        """Return the first valid YouTube URL from the text input."""
        for url in extract_urls(self.youtube_url.value or ""):
            if is_youtube_url(url):
                return url
        return None

    def resolved_artist(self, playlist_title: str | None) -> str:
        """Resolve the artist value used for downloaded tracks."""
        artist_input = (self.artist.value or "").strip()
        return artist_input or playlist_title or "YouTube"

    def resolved_name(self, track: Path, index: int) -> str:
        """Resolve the stored display name for a downloaded track."""
        custom_name = (self.song_name.value or "").strip()
        if index == 0 and custom_name:
            return custom_name
        return track.stem

    def summarize_downloads(
        self,
        downloaded: list[Path],
        context: _DownloadSummaryContext,
    ) -> tuple[list[str], list[str]]:
        """Store downloaded songs and return success and blocked status lines."""
        lines: list[str] = []
        blocked_lines: list[str] = []
        for index, track in enumerate(downloaded):
            if not context.is_manager and track.name in context.disabled_filenames:
                blocked_lines.append(
                    "⛔ "
                    f"**{track.stem}** was previously disabled and can only be added "
                    "by a **Music Manager**."
                )
                continue
            name = self.resolved_name(track, index)
            try:
                song_id = add_song(name, context.artist, track.name, context.added_by)
            except ValueError:
                blocked_lines.append(f"⛔ Skipped invalid filename: `{track.name}`.")
                continue
            lines.append(f"✅ Added **{name}** (ID: `{song_id}`)")
        return lines, blocked_lines

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        """Download tracks from YouTube and add them to the library."""
        youtube_url = self.first_youtube_url()
        if youtube_url is None:
            await interaction.response.send_message(
                "❌ Please provide a valid YouTube URL.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            downloaded, playlist_title = await asyncio.to_thread(
                download_youtube_audio,
                youtube_url,
                SONGS_DIR,
                False,
            )
        except (DownloadError, OSError) as exc:
            await interaction.followup.send(
                f"❌ Could not download from YouTube: {exc}",
                ephemeral=True,
            )
            return

        if not downloaded:
            await interaction.followup.send(
                "❌ Downloaded file type is not supported.\n"
                f"Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
                ephemeral=True,
            )
            return

        lines, blocked_lines = self.summarize_downloads(
            downloaded,
            _DownloadSummaryContext(
                added_by=str(interaction.user.id),
                is_manager=is_music_manager(interaction),
                disabled_filenames=get_disabled_song_filenames(),
                artist=self.resolved_artist(playlist_title),
            ),
        )
        if not lines and blocked_lines:
            await interaction.followup.send("\n".join(blocked_lines), ephemeral=True)
            return
        if blocked_lines:
            lines.extend(blocked_lines)
        await interaction.followup.send("\n".join(lines), ephemeral=True)


class DeleteSongModal(discord.ui.Modal, title="Delete Song"):
    """Delete or deactivate a song matched by its name."""

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

    def delete_mode(self) -> bool | None:
        """Return True for hard delete, False for disable, or None for invalid input."""
        raw_choice = (self.hard_delete.value or "").strip().lower()
        if raw_choice not in {"y", "yes", "n", "no"}:
            return None
        return raw_choice in {"y", "yes"}

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        """Delete or deactivate a song selected by name."""
        if not await require_music_manager(interaction, action="delete songs"):
            return

        query = self.song_name.value
        matches = get_songs_by_name(query)
        if not matches:
            await interaction.response.send_message(
                f"Sorry, there is no song named **{query}**.",
                ephemeral=True,
            )
            return

        should_hard_delete = self.delete_mode()
        if should_hard_delete is None:
            await interaction.response.send_message(
                "❌ Enter **Y** to fully delete the song or **N** to disable it.",
                ephemeral=True,
            )
            return

        if len(matches) > 1:
            usernames = await asyncio.gather(
                *[
                    _resolve_username(
                        interaction.client,
                        match.get("added_by", ""),
                        interaction.guild,
                    )
                    for match in matches
                ]
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


class DeleteSongByIdModal(discord.ui.Modal, title="Delete / Disable Song by ID"):
    """Delete or deactivate a song selected by its numeric ID."""

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

    def delete_mode(self) -> bool | None:
        """Return True for hard delete, False for disable, or None for invalid input."""
        raw_choice = (self.hard_delete.value or "").strip().lower()
        if raw_choice not in {"y", "yes", "n", "no"}:
            return None
        return raw_choice in {"y", "yes"}

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        """Delete or deactivate a song selected by ID."""
        if not await require_music_manager(interaction, action="delete songs"):
            return

        try:
            song_id = int(self.song_id.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Song ID must be a number.",
                ephemeral=True,
            )
            return

        song = await _get_song_or_respond_missing(interaction, song_id)
        if not song:
            return

        should_hard_delete = self.delete_mode()
        if should_hard_delete is None:
            await interaction.response.send_message(
                "❌ Enter **Y** to fully delete the song or **N** to disable it.",
                ephemeral=True,
            )
            return

        if should_hard_delete:
            hard_delete_song(song["id"])
            song_path = SONGS_DIR / song["filename"]
            if song_path.exists():
                song_path.unlink()
            await interaction.response.send_message(
                "🗑️ Permanently deleted "
                f"**{song['name']}** (ID: `{song['id']}`) by **{song['artist']}**.",
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
                f"⛔ Disabled **{song['name']}** (ID: `{song['id']}`) "
                f"by **{song['artist']}**.\n"
                "Use `/toggle_song_id` to re-enable it."
            ),
            ephemeral=True,
        )
