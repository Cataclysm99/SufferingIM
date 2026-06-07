"""Slash commands for daily genre filtering."""

from __future__ import annotations

import discord
from discord import app_commands

from app_bot import MusicBot
from database import (
    disable_daily_genres,
    enable_daily_genres,
    get_daily_genre_filter,
    serialize_genre_names,
)


def _normalized_genre_input(genres: str) -> str:
    """Return canonical comma-separated genres from raw user input."""
    return serialize_genre_names(genres)


def _daily_genre_status_text(state: dict) -> str:
    """Describe the current daily genre filter state for command replies."""
    genres = state.get("genres", [])
    mode = state.get("mode", "all")
    if mode == "include" and genres:
        return (
            "Enabled today: "
            f"**{', '.join(genres)}**.\n"
            "All other tagged genres are excluded for today."
        )
    if mode == "exclude" and genres:
        return (
            "Disabled today: "
            f"**{', '.join(genres)}**.\n"
            "Songs stay playable unless all of their tagged genres are disabled."
        )
    return "All tagged genres are currently enabled for today."


def register_genre_commands(bot: MusicBot) -> None:
    """Register public commands for today's genre filter state."""

    @bot.tree.command(
        name="enable_genres",
        description="Enable only these genres, or re-enable ones you disabled earlier.",
    )
    @app_commands.describe(genres="Comma-separated genres, for example: rock, synthwave")
    async def cmd_enable_genres(interaction: discord.Interaction, genres: str) -> None:
        normalized = _normalized_genre_input(genres)
        if not normalized:
            await interaction.response.send_message(
                "❌ Provide at least one genre.",
                ephemeral=True,
            )
            return
        state = enable_daily_genres(normalized)
        await interaction.response.send_message(
            "✅ Updated today's enabled genres.\n" + _daily_genre_status_text(state),
            ephemeral=True,
        )

    @bot.tree.command(
        name="disable_genres",
        description="Disable these genres for today while keeping all other genres active.",
    )
    @app_commands.describe(genres="Comma-separated genres, for example: rock, synthwave")
    async def cmd_disable_genres(interaction: discord.Interaction, genres: str) -> None:
        normalized = _normalized_genre_input(genres)
        if not normalized:
            await interaction.response.send_message(
                "❌ Provide at least one genre.",
                ephemeral=True,
            )
            return
        state = disable_daily_genres(normalized)
        await interaction.response.send_message(
            "✅ Updated today's disabled genres.\n" + _daily_genre_status_text(state),
            ephemeral=True,
        )

    @bot.tree.command(
        name="genres_today",
        description="Show the current daily genre filter state.",
    )
    async def cmd_genres_today(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            _daily_genre_status_text(get_daily_genre_filter()),
            ephemeral=True,
        )
