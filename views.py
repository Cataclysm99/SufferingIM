"""
views.py – Persistent Discord UI components for the music bot.

MusicControlView
    Persistent button panel (timeout=None) that survives bot restarts.
    All button custom_ids are stable strings prefixed with "music:".

    Row 0 – playback controls : Join & Play | Pause | Resume | Skip
    Row 1 – library controls  : Song List  | Add Song | Delete Song

AddSongModal
    Modal form to register an audio file that already exists in songs/.

DeleteSongModal
    Modal form to remove a song by its unique id.
"""
from __future__ import annotations

import discord

from config import SONGS_DIR
from database import add_song, delete_song, get_all_songs

# Column widths used in the song-table display.
_COL_ID = 5
_COL_NAME = 30
_COL_AUTHOR = 20
# Number of history messages to scan when searching for an existing controller.
CONTROLLER_SEARCH_LIMIT = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _song_table_embed(songs: list[dict]) -> discord.Embed:
    """Build a nicely formatted embed table for the song library."""
    embed = discord.Embed(title="🎵 Song Library", colour=discord.Colour.blue())

    if not songs:
        embed.description = "*No songs in the library yet.*"
        return embed

    header = f"{'ID':<{_COL_ID}} {'Name':<{_COL_NAME}} {'Author':<{_COL_AUTHOR}} {'Plays'}"
    divider = "─" * (_COL_ID + _COL_NAME + _COL_AUTHOR + 10)
    rows = [
        f"{s['id']:<{_COL_ID}} {s['name'][:_COL_NAME - 2]:<{_COL_NAME}} {s['author'][:_COL_AUTHOR - 2]:<{_COL_AUTHOR}} {s['times_played']}"
        for s in songs
    ]
    embed.description = "```\n" + "\n".join([header, divider, *rows]) + "\n```"
    embed.set_footer(text=f"{len(songs)} song(s) total")
    return embed


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class AddSongModal(discord.ui.Modal, title="Add Song"):
    """Collect song metadata and register the file in the database."""

    song_name = discord.ui.TextInput(
        label="Song Name",
        placeholder="e.g. Bohemian Rhapsody",
        max_length=100,
    )
    author = discord.ui.TextInput(
        label="Author / Artist",
        placeholder="e.g. Queen",
        max_length=100,
    )
    filename = discord.ui.TextInput(
        label="Filename (must exist in songs/ folder)",
        placeholder="e.g. bohemian_rhapsody.mp3",
        max_length=255,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        song_path = SONGS_DIR / self.filename.value
        if not song_path.exists():
            await interaction.response.send_message(
                f"❌ `{self.filename.value}` was not found inside the `songs/` folder.\n"
                "Upload the file first (via `/upload_song`) or check the filename.",
                ephemeral=True,
            )
            return

        song_id = add_song(
            self.song_name.value, self.author.value, self.filename.value
        )
        await interaction.response.send_message(
            f"✅ **{self.song_name.value}** by **{self.author.value}** added to the library.\n"
            f"Song ID: `{song_id}`",
            ephemeral=True,
        )


class DeleteSongModal(discord.ui.Modal, title="Delete Song"):
    """Remove a song from the library by its id."""

    song_id = discord.ui.TextInput(
        label="Song ID",
        placeholder="Enter the numeric song ID",
        max_length=10,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.song_id.value.strip()
        if not raw.isdigit():
            await interaction.response.send_message(
                "❌ Song ID must be a positive integer.", ephemeral=True
            )
            return

        sid = int(raw)
        song = delete_song(sid)
        if song is None:
            await interaction.response.send_message(
                f"❌ No song found with ID `{sid}`.", ephemeral=True
            )
            return

        # Remove the audio file from disk.
        song_path = SONGS_DIR / song["filename"]
        if song_path.exists():
            song_path.unlink()

        await interaction.response.send_message(
            f"🗑️ **{song['name']}** (ID {sid}) has been deleted from the library.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Persistent controller view
# ---------------------------------------------------------------------------

class MusicControlView(discord.ui.View):
    """
    Persistent button panel for music playback and library management.

    Because this view has ``timeout=None`` and all custom_ids are static
    it survives bot restarts when re-registered with ``bot.add_view()``.
    The player is retrieved at interaction time via ``interaction.client.player``
    so no state needs to be stored on the view itself.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    # ------------------------------------------------------------------
    # Row 0 – playback
    # ------------------------------------------------------------------

    @discord.ui.button(
        label="▶ Join & Play",
        style=discord.ButtonStyle.success,
        custom_id="music:join_play",
        row=0,
    )
    async def join_play(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]

        if not interaction.user.voice:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "❌ You must be in a voice channel first!", ephemeral=True
            )
            return

        voice_channel = interaction.user.voice.channel  # type: ignore[union-attr]
        await player.connect(voice_channel)

        if player.is_active():
            await interaction.response.send_message(
                "✅ Joined the voice channel. Music is already playing.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        song = await player.play_next()

        if song:
            await interaction.followup.send(
                f"🎵 Now playing: **{song['name']}** by **{song['author']}**",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ The song library is empty. Add songs with **Add Song**.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="⏸ Pause",
        style=discord.ButtonStyle.primary,
        custom_id="music:pause",
        row=0,
    )
    async def pause(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]
        if player.pause():
            await interaction.response.send_message("⏸ Paused.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ Nothing is playing right now.", ephemeral=True
            )

    @discord.ui.button(
        label="▶ Resume",
        style=discord.ButtonStyle.primary,
        custom_id="music:resume",
        row=0,
    )
    async def resume(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]
        if player.resume():
            await interaction.response.send_message("▶ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ Nothing is paused right now.", ephemeral=True
            )

    @discord.ui.button(
        label="⏭ Skip",
        style=discord.ButtonStyle.secondary,
        custom_id="music:skip",
        row=0,
    )
    async def skip(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]
        if player.skip():
            await interaction.response.send_message("⏭ Skipped.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ Nothing to skip.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # Row 1 – library management
    # ------------------------------------------------------------------

    @discord.ui.button(
        label="📋 Song List",
        style=discord.ButtonStyle.secondary,
        custom_id="music:song_list",
        row=1,
    )
    async def song_list(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        songs = get_all_songs()
        embed = _song_table_embed(songs)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="➕ Add Song",
        style=discord.ButtonStyle.success,
        custom_id="music:add_song",
        row=1,
    )
    async def add_song(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(AddSongModal())

    @discord.ui.button(
        label="🗑 Delete Song",
        style=discord.ButtonStyle.danger,
        custom_id="music:delete_song",
        row=1,
    )
    async def delete_song(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(DeleteSongModal())
