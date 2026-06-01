"""Interactive song-management view with per-song editing controls."""

from __future__ import annotations

from dataclasses import dataclass

import discord

from database import (
    parse_genre_names,
    serialize_genre_names,
    update_song_metadata,
)

from .helpers import _resolve_username

MANAGE_SONGS_PAGE_SIZE = 25


def _availability_label(is_available: bool) -> str:
    """Return the display label for a song availability flag."""
    return "Enabled" if is_available else "Disabled"


@dataclass(slots=True)
class _PendingSongUpdate:
    """Staged unsaved song edits for one song record."""

    name: str
    description: str
    genres: str
    available: bool


class _ManageSongsSelect(discord.ui.Select):
    """Dropdown used to choose the song shown in the manager view."""

    def __init__(self, parent_view: ManageSongsView) -> None:
        super().__init__(placeholder="Select a song to manage", row=0)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Switch the selected song and refresh the embed."""
        self.parent_view.message = interaction.message
        self.parent_view.selected_song_id = int(self.values[0])
        self.parent_view.refresh_controls()
        await interaction.response.edit_message(
            embed=await self.parent_view.build_embed(interaction.client, interaction.guild),
            view=self.parent_view,
        )


class ManageSongEditModal(discord.ui.Modal, title="Edit Song"):
    """Stage editable song metadata before the user saves it."""

    name = discord.ui.TextInput(label="Song Name", max_length=100)
    description = discord.ui.TextInput(
        label="Description",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500,
    )
    genres = discord.ui.TextInput(
        label="Genres (comma-separated)",
        required=False,
        max_length=200,
    )
    available = discord.ui.TextInput(
        label="Available? (Y/N)",
        max_length=3,
    )

    def __init__(self, parent_view: ManageSongsView) -> None:
        super().__init__()
        self.parent_view = parent_view
        snapshot = parent_view.selected_song_snapshot
        self.name.default = str(snapshot["name"])
        self.description.default = str(snapshot.get("description", ""))
        self.genres.default = str(snapshot.get("genres", ""))
        self.available.default = "Y" if snapshot.get("available", 1) else "N"

    async def on_submit(self, interaction: discord.Interaction, /) -> None:
        """Stage the modal values and refresh the management view."""
        raw_available = (self.available.value or "").strip().lower()
        if raw_available not in {"y", "yes", "n", "no"}:
            await interaction.response.send_message(
                "❌ Availability must be **Y** or **N**.",
                ephemeral=True,
            )
            return
        self.parent_view.stage_update(
            name=self.name.value.strip(),
            description=self.description.value.strip(),
            genres=serialize_genre_names(self.genres.value),
            available=raw_available in {"y", "yes"},
        )
        await interaction.response.send_message(
            "📝 Changes staged. Press **Save** to apply them.",
            ephemeral=True,
        )
        await self.parent_view.refresh_message(interaction.client, interaction.guild)


class ManageSongsView(discord.ui.View):
    """Paginated manager UI for reviewing and editing songs."""

    def __init__(self, songs: list[dict], requester_id: int) -> None:
        super().__init__(timeout=600)
        self.requester_id = requester_id
        self.message: discord.Message | None = None
        self._songs = songs
        self._pending_updates: dict[int, _PendingSongUpdate] = {}
        self._page = 0
        self.selected_song_id = int(songs[0]["id"])
        self.song_select = _ManageSongsSelect(self)
        self.add_item(self.song_select)
        self.refresh_controls()

    @property
    def total_pages(self) -> int:
        """Return the number of dropdown pages required for the song list."""
        return max(1, -(-len(self._songs) // MANAGE_SONGS_PAGE_SIZE))

    @property
    def selected_song(self) -> dict:
        """Return the currently selected persisted song record."""
        for song in self._songs:
            if int(song["id"]) == self.selected_song_id:
                return song
        return self._songs[0]

    @property
    def selected_song_snapshot(self) -> dict:
        """Return the selected song with any pending edits applied."""
        song = dict(self.selected_song)
        pending = self._pending_updates.get(self.selected_song_id)
        if pending is None:
            return song
        song.update(
            {
                "name": pending.name,
                "description": pending.description,
                "genres": pending.genres,
                "available": 1 if pending.available else 0,
            }
        )
        return song

    def _page_songs(self) -> list[dict]:
        """Return the songs visible on the current dropdown page."""
        start = self._page * MANAGE_SONGS_PAGE_SIZE
        return self._songs[start : start + MANAGE_SONGS_PAGE_SIZE]

    def refresh_controls(self) -> None:
        """Refresh the dropdown options and button states."""
        page_songs = self._page_songs()
        if page_songs and self.selected_song_id not in {int(song["id"]) for song in page_songs}:
            self.selected_song_id = int(page_songs[0]["id"])
        self.song_select.options = [
            discord.SelectOption(
                label=f"{song['id']}: {song['name'][:90]}",
                value=str(song["id"]),
                description=(
                    f"{_availability_label(bool(song.get('available', 1)))}"
                    f" • {song.get('genres', '') or 'No genres'}"
                )[:100],
                default=int(song["id"]) == self.selected_song_id,
            )
            for song in page_songs
        ]
        self.previous_page.disabled = self._page <= 0
        self.next_page.disabled = self._page >= self.total_pages - 1
        self.save_song.disabled = self.selected_song_id not in self._pending_updates

    def stage_update(
        self,
        *,
        name: str,
        description: str,
        genres: str,
        available: bool,
    ) -> None:
        """Store or discard the selected song's staged updates."""
        song = self.selected_song
        normalized_name = name.strip()
        normalized_description = description.strip()
        normalized_genres = serialize_genre_names(genres)
        original_genres = serialize_genre_names(song.get("genres", ""))
        if (
            normalized_name == str(song["name"]).strip()
            and normalized_description == str(song.get("description", "")).strip()
            and normalized_genres == original_genres
            and available == bool(song.get("available", 1))
        ):
            self._pending_updates.pop(self.selected_song_id, None)
        else:
            self._pending_updates[self.selected_song_id] = _PendingSongUpdate(
                name=normalized_name,
                description=normalized_description,
                genres=normalized_genres,
                available=available,
            )
        self.refresh_controls()

    async def build_embed(
        self,
        client: discord.Client,
        guild: discord.Guild | None,
    ) -> discord.Embed:
        """Build the embed for the currently selected song."""
        song = self.selected_song_snapshot
        pending = self._pending_updates.get(self.selected_song_id)
        added_by_display = await _resolve_username(client, str(song.get("added_by", "")), guild)
        embed = discord.Embed(
            title=f"🎛️ Manage Song #{song['id']}",
            colour=discord.Colour.dark_teal(),
        )
        embed.add_field(name="Name", value=str(song["name"]) or "Unknown", inline=False)
        embed.add_field(name="Artist", value=str(song.get("artist", "")) or "Unknown", inline=True)
        embed.add_field(
            name="Availability",
            value=_availability_label(bool(song.get("available", 1))),
            inline=True,
        )
        embed.add_field(name="Times Played", value=str(song.get("times_played", 0)), inline=True)
        embed.add_field(
            name="Genres",
            value=", ".join(parse_genre_names(song.get("genres", ""))) or "None",
            inline=False,
        )
        embed.add_field(
            name="Description",
            value=str(song.get("description", "")).strip() or "None",
            inline=False,
        )
        embed.add_field(
            name="Filename",
            value=str(song.get("filename", "")) or "Unknown",
            inline=False,
        )
        embed.add_field(name="Last Added/Edited By", value=added_by_display, inline=False)
        footer = f"Page {self._page + 1}/{self.total_pages}"
        if pending is not None:
            footer += " • unsaved changes"
        embed.set_footer(text=footer)
        return embed

    async def refresh_message(
        self,
        client: discord.Client,
        guild: discord.Guild | None,
    ) -> None:
        """Refresh the stored message after a modal updates the staged state."""
        if self.message is None:
            return
        await self.message.edit(embed=await self.build_embed(client, guild), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Restrict song-management interactions to the user who opened the view."""
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "❌ Only the user who opened this menu can manage these songs.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="◀ Prev Page", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        """Show the previous song-select page."""
        self.message = interaction.message
        if self._page > 0:
            self._page -= 1
        self.refresh_controls()
        await interaction.response.edit_message(
            embed=await self.build_embed(interaction.client, interaction.guild),
            view=self,
        )

    @discord.ui.button(label="Next Page ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        """Show the next song-select page."""
        self.message = interaction.message
        if self._page < self.total_pages - 1:
            self._page += 1
        self.refresh_controls()
        await interaction.response.edit_message(
            embed=await self.build_embed(interaction.client, interaction.guild),
            view=self,
        )

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, row=1)
    async def edit_song(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        """Open the staging modal for the selected song."""
        self.message = interaction.message
        await interaction.response.send_modal(ManageSongEditModal(self))

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=1)
    async def save_song(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        """Persist the selected song's staged changes."""
        self.message = interaction.message
        pending = self._pending_updates.get(self.selected_song_id)
        if pending is None:
            await interaction.response.send_message(
                "ℹ️ There are no staged changes to save.",
                ephemeral=True,
            )
            return
        updated = update_song_metadata(
            self.selected_song_id,
            {
                "name": pending.name,
                "description": pending.description,
                "genres": pending.genres,
                "available": pending.available,
            },
            added_by=str(interaction.user.id),
        )
        if updated is None:
            await interaction.response.send_message(
                "❌ That song could not be found anymore.",
                ephemeral=True,
            )
            return
        for index, song in enumerate(self._songs):
            if int(song["id"]) != self.selected_song_id:
                continue
            self._songs[index] = updated
            break
        self._pending_updates.pop(self.selected_song_id, None)
        self.refresh_controls()
        await interaction.response.edit_message(
            embed=await self.build_embed(interaction.client, interaction.guild),
            view=self,
        )
        await interaction.followup.send(
            f"✅ Saved changes to **{updated['name']}**.",
            ephemeral=True,
        )
