"""Slash commands for managing yt-dlp cookie authentication."""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord import app_commands

from app_bot import MusicBot
from config import BASE_DIR, YTDLP_COOKIES_FILE
from media_utils import parse_cookie_file_info
from views import require_music_manager

log = logging.getLogger(__name__)


def _cookie_save_path() -> Path:
    """Return the path where /update_cookies writes the cookies file."""
    if YTDLP_COOKIES_FILE:
        return Path(YTDLP_COOKIES_FILE).expanduser()
    return BASE_DIR / "cookies.txt"


def register_cookie_commands(bot: MusicBot) -> None:
    """Register yt-dlp cookie management commands."""

    @bot.tree.command(
        name="update_cookies",
        description="Replace the yt-dlp cookies file from an attachment (Music Manager only).",
    )
    @app_commands.describe(
        file="Netscape-format cookies.txt exported from your browser"
    )
    async def cmd_update_cookies(
        interaction: discord.Interaction,
        file: discord.Attachment,
    ) -> None:
        if not await require_music_manager(interaction, action="update cookies"):
            return
        if not file.filename.lower().endswith(".txt"):
            await interaction.response.send_message(
                "❌ Please attach a `.txt` cookies file (Netscape format).",
                ephemeral=True,
            )
            return
        if file.size > 2 * 1024 * 1024:
            await interaction.response.send_message(
                "❌ File is too large (max 2 MB). A valid cookies file should be much smaller.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        content_bytes = await file.read()
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            await interaction.followup.send(
                "❌ File is not valid UTF-8 text. Export the cookies file from your browser "
                "again and try once more.",
                ephemeral=True,
            )
            return
        is_valid, summary, _auth = parse_cookie_file_info(content)
        if not is_valid:
            await interaction.followup.send(
                f"❌ The file does not look like a valid Netscape cookies file.\n{summary}\n\n"
                "Export it using a browser extension such as *Get cookies.txt LOCALLY* "
                "and ensure you export from a YouTube page while logged in.",
                ephemeral=True,
            )
            return
        save_path = _cookie_save_path()
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            await interaction.followup.send(
                f"❌ Failed to save cookies file: {exc}",
                ephemeral=True,
            )
            return
        log.info("yt-dlp cookies file updated by %s → %s", interaction.user, save_path)
        hint = ""
        if not YTDLP_COOKIES_FILE:
            hint = (
                f"\n\n⚠️ `YTDLP_COOKIES_FILE` is not set. "
                f"Add `YTDLP_COOKIES_FILE={save_path.name}` to your `.env` and restart "
                "the bot so yt-dlp will use this file."
            )
        await interaction.followup.send(
            f"✅ Cookies file saved to `{save_path.name}`.\n{summary}{hint}",
            ephemeral=True,
        )
