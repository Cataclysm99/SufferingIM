"""Helper utilities for embeds and user-resolution in views."""

from __future__ import annotations

import asyncio

import discord

from .constants import COL_ADDED_BY, COL_ARTIST, COL_ID, COL_NAME, REACT_DEACTIVATE, REACT_HARD_DELETE


async def _resolve_username(
    client: discord.Client,
    user_id_str: str,
    guild: discord.Guild | None = None,
) -> str:
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
            except Exception:
                member = None
        if member is not None:
            return member.display_name

    user = client.get_user(uid)
    if user is None:
        try:
            user = await client.fetch_user(uid)
        except Exception:
            return user_id_str
    return user.display_name


async def _build_delete_confirm_message(
    song: dict,
    requester_id: int,
    client: discord.Client,
    guild: discord.Guild | None = None,
) -> str:
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
        f"— removes from the database **and** deletes the audio file.\n\n"
        f"Only <@{requester_id}> can confirm this action."
    )


async def _song_table_embed(
    songs: list[dict],
    title: str = "🎵 Song Library",
    *,
    client: discord.Client | None = None,
    guild: discord.Guild | None = None,
    compact: bool = False,
) -> discord.Embed:
    embed = discord.Embed(title=title, colour=discord.Colour.blue())

    if not songs:
        embed.description = "*No songs in the library yet.*"
        return embed

    if compact:
        header = (
            f"{'ID':<{COL_ID}} "
            f"{'Name':<{COL_NAME}} "
            f"{'Artist':<{COL_ARTIST}}"
        )
        divider = "─" * (COL_ID + COL_NAME + COL_ARTIST + 2)
        rows = [
            f"{s['id']:<{COL_ID}} "
            f"{s['name'][:COL_NAME]:<{COL_NAME}} "
            f"{s['artist'][:COL_ARTIST]:<{COL_ARTIST}}"
            for s in songs
        ]
    else:
        show_inactive_marker = any(not s.get("available", 1) for s in songs)
        header = (
            f"{'ID':<{COL_ID}} "
            f"{'Name':<{COL_NAME}} "
            f"{'Artist':<{COL_ARTIST}} "
            f"{'Added By':<{COL_ADDED_BY}} "
            f"Plays"
        )
        divider = "─" * (COL_ID + COL_NAME + COL_ARTIST + COL_ADDED_BY + 18)
        added_by_ids = [str(s.get("added_by", "")) for s in songs]
        if client:
            display_names: list[str] = list(
                await asyncio.gather(*[_resolve_username(client, uid, guild) for uid in added_by_ids])
            )
        else:
            display_names = added_by_ids

        rows = []
        for s, added_by_str in zip(songs, display_names):
            name_str = s["name"]
            if show_inactive_marker and not s.get("available", 1):
                name_str = f"[inactive] {name_str}"
            name_str = name_str[:COL_NAME]
            rows.append(
                f"{s['id']:<{COL_ID}} "
                f"{name_str:<{COL_NAME}} "
                f"{s['artist'][:COL_ARTIST]:<{COL_ARTIST}} "
                f"{added_by_str[:COL_ADDED_BY]:<{COL_ADDED_BY}} "
                f"{s['times_played']}"
            )

    max_desc_len = 4096
    shown_rows: list[str] = []
    for row in rows:
        candidate = "```\n" + "\n".join([header, divider, *shown_rows, row]) + "\n```"
        if len(candidate) > max_desc_len:
            break
        shown_rows.append(row)

    hidden_count = len(rows) - len(shown_rows)
    suffix_line = f"... ({hidden_count} more song(s) not shown)"
    if hidden_count:
        while True:
            lines = [header, divider, *shown_rows, suffix_line]
            candidate = "```\n" + "\n".join(lines) + "\n```"
            if len(candidate) <= max_desc_len:
                break
            if not shown_rows:
                lines = [header, divider]
                candidate = "```\n" + "\n".join(lines) + "\n```"
                break
            shown_rows.pop()

    final_lines = [header, divider, *shown_rows]
    if hidden_count:
        final_lines.append(suffix_line)
    embed.description = "```\n" + "\n".join(final_lines) + "\n```"
    if hidden_count:
        embed.set_footer(text=f"{len(songs)} song(s) total • showing {len(shown_rows)}")
    else:
        embed.set_footer(text=f"{len(songs)} song(s) total")
    return embed

