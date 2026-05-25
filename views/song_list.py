"""Ephemeral paginated song list view."""

from __future__ import annotations

import discord

from .constants import COL_ARTIST, COL_ID, COL_NAME, SONGS_PER_PAGE


def _song_page_embed(songs: list[dict], page: int, total_pages: int) -> discord.Embed:
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
    body = "```\n" + "\n".join([header, divider, *rows]) + "\n```"
    embed = discord.Embed(
        title="🎵 Song Playlist",
        description=body,
        colour=discord.Colour.blue(),
    )
    embed.set_footer(text=f"Page {page}/{total_pages} • {len(songs)} song(s) on this page")
    return embed


class SongListView(discord.ui.View):
    def __init__(self, songs: list[dict]) -> None:
        super().__init__(timeout=600)
        self._songs = songs
        self._page = 1
        self._total_pages = max(1, -(-len(songs) // SONGS_PER_PAGE))

    def _current_page_songs(self) -> list[dict]:
        start = (self._page - 1) * SONGS_PER_PAGE
        return self._songs[start : start + SONGS_PER_PAGE]

    def _build_embed(self) -> discord.Embed:
        return _song_page_embed(
            self._current_page_songs(), self._page, self._total_pages
        )

    def _update_buttons(self) -> None:
        self.prev_button.disabled = self._page <= 1
        self.next_button.disabled = self._page >= self._total_pages

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self._page > 1:
            self._page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self._page < self._total_pages:
            self._page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

