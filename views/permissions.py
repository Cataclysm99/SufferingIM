"""Permission helpers for view interactions."""

from __future__ import annotations

import discord

from config import MUSIC_MANAGER_ROLE_ID


def is_music_manager(interaction: discord.Interaction) -> bool:
    """Return True if the interacting user may manage the song library."""
    if not MUSIC_MANAGER_ROLE_ID:
        return True
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    return any(r.id == MUSIC_MANAGER_ROLE_ID for r in member.roles)
