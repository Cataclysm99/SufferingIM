"""Ephemeral paginated song list view."""

from __future__ import annotations

import discord

from .constants import SONGS_PER_PAGE
from .helpers import _build_compact_song_rows


def _song_page_embed(songs: list[dict], page: int, total_pages: int) -> discord.Embed:
    """Build the playlist embed for a single page of songs."""
    header, divider, rows = _build_compact_song_rows(songs)
    body = "```\n" + "\n".join([header, divider, *rows]) + "\n```"
    embed = discord.Embed(
        title="🎵 Song Playlist",
        description=body,
        colour=discord.Colour.blue(),
    )
    embed.set_footer(text=f"Page {page}/{total_pages} • {len(songs)} song(s) on this page")
    return embed


class SongListView(discord.ui.View):
    """Paginate the song library inside an ephemeral button view."""

    def __init__(self, songs: list[dict]) -> None:
        super().__init__(timeout=600)
        self._songs = songs
        self._page = 1
        self._total_pages = max(1, -(-len(songs) // SONGS_PER_PAGE))

    def _current_page_songs(self) -> list[dict]:
        """Return the songs shown on the current page."""
        start = (self._page - 1) * SONGS_PER_PAGE
        return self._songs[start : start + SONGS_PER_PAGE]

    def build_embed(self) -> discord.Embed:
        """Build the current page embed."""
        return _song_page_embed(self._current_page_songs(), self._page, self._total_pages)

    def update_buttons(self) -> None:
        """Refresh button disabled states for the current page."""
        self.prev_button.disabled = self._page <= 1
        self.next_button.disabled = self._page >= self._total_pages

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        """Handle a request to show the previous page."""
        if self._page > 1:
            self._page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        """Handle a request to show the next page."""
        if self._page < self._total_pages:
            self._page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)
