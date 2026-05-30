"""Helper utilities for embeds and user-resolution in views."""

from __future__ import annotations

import asyncio

import discord

from database import get_song

from .constants import (
    COL_ADDED_BY,
    COL_ARTIST,
    COL_ID,
    COL_NAME,
    REACT_CANCEL,
    REACT_DEACTIVATE,
    REACT_HARD_DELETE,
)


async def _resolve_username(
    client: discord.Client,
    user_id_str: str,
    guild: discord.Guild | None = None,
) -> str:
    """Resolve a user ID string into a display name when possible."""
    if not user_id_str:
        return "Unknown"
    try:
        uid = int(user_id_str)
    except ValueError:
        return user_id_str

    if guild is not None:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                member = None
        if member is not None:
            return member.display_name

    user = client.get_user(uid)
    if user is None:
        try:
            user = await client.fetch_user(uid)
        except discord.HTTPException:
            return user_id_str
    return user.display_name


async def _build_delete_confirm_message(
    song: dict,
    requester_id: int,
    client: discord.Client,
    guild: discord.Guild | None = None,
) -> str:
    """Build the legacy reaction-based delete confirmation message."""
    added_by_display = await _resolve_username(client, song.get("added_by", ""), guild)
    status = "✅ active" if song.get("available", 1) else "⛔ deactivated"
    return (
        f"🎵 **{song['name']}** by **{song['artist']}** "
        f"(ID: `{song['id']}`, {status})\n"
        f"Added by: **{added_by_display}**\n\n"
        f"React with {REACT_DEACTIVATE} to **deactivate** "
        f"— removes from playlist but keeps the audio file "
        f"(re-enable later with `/toggle_song` or `/toggle_song_id`).\n"
        f"React with {REACT_HARD_DELETE} to **permanently delete** "
        f"— removes from the database **and** deletes the audio file.\n"
        f"React with {REACT_CANCEL} to **cancel** this action.\n\n"
        f"Only <@{requester_id}> can confirm this action."
    )


async def _send_missing_song_id(
    interaction: discord.Interaction,
    song_id: int,
) -> None:
    """Send the standard song-not-found message for a numeric ID lookup."""
    await interaction.response.send_message(
        f"Sorry, there is no song with ID **{song_id}**.",
        ephemeral=True,
    )


async def _get_song_or_respond_missing(
    interaction: discord.Interaction,
    song_id: int,
) -> dict | None:
    """Fetch a song by ID, replying with the standard error when it is missing."""
    song = get_song(song_id)
    if song:
        return song
    await _send_missing_song_id(interaction, song_id)
    return None


def _build_compact_song_rows(songs: list[dict]) -> tuple[str, str, list[str]]:
    """Build compact header, divider, and rows for song-list displays."""
    header = (
        f"{'ID':<{COL_ID}} "
        f"{'Name':<{COL_NAME}} "
        f"{'Artist':<{COL_ARTIST}}"
    )
    divider = "─" * (COL_ID + COL_NAME + COL_ARTIST + 2)
    rows = [
        f"{song['id']:<{COL_ID}} "
        f"{song['name'][:COL_NAME]:<{COL_NAME}} "
        f"{song['artist'][:COL_ARTIST]:<{COL_ARTIST}}"
        for song in songs
    ]
    return header, divider, rows


async def _build_full_song_rows(
    songs: list[dict],
    client: discord.Client | None,
    guild: discord.Guild | None,
) -> tuple[str, str, list[str]]:
    """Build full song-table rows including uploader and play count columns."""
    show_inactive_marker = any(not song.get("available", 1) for song in songs)
    header = (
        f"{'ID':<{COL_ID}} "
        f"{'Name':<{COL_NAME}} "
        f"{'Artist':<{COL_ARTIST}} "
        f"{'Added By':<{COL_ADDED_BY}} "
        "Plays"
    )
    divider = "─" * (COL_ID + COL_NAME + COL_ARTIST + COL_ADDED_BY + 18)
    added_by_ids = [str(song.get("added_by", "")) for song in songs]
    display_names = added_by_ids
    if client is not None:
        display_names = list(
            await asyncio.gather(
                *[_resolve_username(client, user_id, guild) for user_id in added_by_ids]
            )
        )

    rows: list[str] = []
    for song, added_by_str in zip(songs, display_names):
        name_str = song["name"]
        if show_inactive_marker and not song.get("available", 1):
            name_str = f"[inactive] {name_str}"
        rows.append(
            f"{song['id']:<{COL_ID}} "
            f"{name_str[:COL_NAME]:<{COL_NAME}} "
            f"{song['artist'][:COL_ARTIST]:<{COL_ARTIST}} "
            f"{added_by_str[:COL_ADDED_BY]:<{COL_ADDED_BY}} "
            f"{song['times_played']}"
        )
    return header, divider, rows


def _fit_rows_to_embed(
    header: str,
    divider: str,
    rows: list[str],
    *,
    max_desc_len: int = 4096,
) -> tuple[list[str], int]:
    """Trim rows so the rendered code block fits inside an embed description."""
    shown_rows: list[str] = []
    for row in rows:
        candidate = "```\n" + "\n".join([header, divider, *shown_rows, row]) + "\n```"
        if len(candidate) > max_desc_len:
            break
        shown_rows.append(row)

    hidden_count = len(rows) - len(shown_rows)
    if not hidden_count:
        return shown_rows, 0

    suffix_line = f"... ({hidden_count} more song(s) not shown)"
    while shown_rows:
        lines = [header, divider, *shown_rows, suffix_line]
        candidate = "```\n" + "\n".join(lines) + "\n```"
        if len(candidate) <= max_desc_len:
            return shown_rows, hidden_count
        shown_rows.pop()
        hidden_count = len(rows) - len(shown_rows)
        suffix_line = f"... ({hidden_count} more song(s) not shown)"
    return [], len(rows)


async def _song_table_embed(
    songs: list[dict],
    title: str = "🎵 Song Library",
    *,
    client: discord.Client | None = None,
    guild: discord.Guild | None = None,
    compact: bool = False,
) -> discord.Embed:
    """Build a song table embed, trimming rows to fit Discord limits."""
    embed = discord.Embed(title=title, colour=discord.Colour.blue())
    if not songs:
        embed.description = "*No songs in the library yet.*"
        return embed

    if compact:
        header, divider, rows = _build_compact_song_rows(songs)
    else:
        header, divider, rows = await _build_full_song_rows(songs, client, guild)

    shown_rows, hidden_count = _fit_rows_to_embed(header, divider, rows)
    final_lines = [header, divider, *shown_rows]
    if hidden_count:
        final_lines.append(f"... ({hidden_count} more song(s) not shown)")
    embed.description = "```\n" + "\n".join(final_lines) + "\n```"
    if hidden_count:
        embed.set_footer(text=f"{len(songs)} song(s) total • showing {len(shown_rows)}")
    else:
        embed.set_footer(text=f"{len(songs)} song(s) total")
    return embed
