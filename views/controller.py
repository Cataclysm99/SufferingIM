"""Persistent controller views."""

from __future__ import annotations

import asyncio
import logging

import discord

from database import get_all_songs

from .modals import AddSongModal, DeleteSongByIdModal, DeleteSongModal
from .permissions import is_music_manager
from .song_list import SongListView

log = logging.getLogger(__name__)


class MusicControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self._play_resume_lock = asyncio.Lock()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        log.error(
            "Unhandled controller view error (item=%s user_id=%s guild_id=%s)",
            getattr(item, "custom_id", None),
            getattr(interaction.user, "id", None),
            getattr(interaction.guild, "id", None),
            exc_info=(type(error), error, error.__traceback__),
        )
        msg = "❌ Something went wrong while handling that button. Please try again."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            return

    @discord.ui.button(
        label="▶ Play",
        style=discord.ButtonStyle.success,
        custom_id="music:play_resume",
        row=0,
    )
    async def play_resume(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]
        async with self._play_resume_lock:
            if player.is_paused():
                player.resume()
                await interaction.response.send_message("▶ Resumed.", ephemeral=True)
                await interaction.client.refresh_controller_status()  # type: ignore[attr-defined]
                return
            if player.is_playing():
                await interaction.response.send_message(
                    "▶ Music is already playing.", ephemeral=True
                )
                return
            if not interaction.user.voice:  # type: ignore[union-attr]
                await interaction.response.send_message(
                    "❌ You must be in a voice channel first!", ephemeral=True
                )
                return

            voice_channel = interaction.user.voice.channel  # type: ignore[union-attr]
            try:
                await player.connect(voice_channel)
            except TimeoutError:
                await interaction.response.send_message(
                    "❌ Timed out joining voice. Please press Play again.",
                    ephemeral=True,
                )
                return
            except discord.ClientException:
                await interaction.response.send_message(
                    "❌ Could not join voice right now. Please try again.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)
            song = await player.play_next()
            if song:
                await interaction.followup.send(
                    f"🎵 Now playing: **{song['name']}** by **{song['artist']}**",
                    ephemeral=True,
                )
            elif player.is_playing():
                await interaction.followup.send("▶ Playback started.", ephemeral=True)
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
            await interaction.client.refresh_controller_status()  # type: ignore[attr-defined]
        else:
            await interaction.response.send_message(
                "❌ Nothing is playing right now.", ephemeral=True
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

    @discord.ui.button(
        label="📞 Leave",
        style=discord.ButtonStyle.danger,
        custom_id="music:leave",
        row=0,
    )
    async def leave(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        player = interaction.client.player  # type: ignore[attr-defined]
        if not player.is_connected():
            await interaction.response.send_message(
                "❌ Not currently in a voice channel.", ephemeral=True
            )
            return
        await player.disconnect()
        await interaction.client.clear_controller_now_playing()  # type: ignore[attr-defined]
        await interaction.response.send_message(
            "📞 Left the voice channel.", ephemeral=True
        )

    @discord.ui.button(
        label="📋 Playlist",
        style=discord.ButtonStyle.secondary,
        custom_id="music:song_list",
        row=1,
    )
    async def song_list(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        songs = get_all_songs()
        if not songs:
            await interaction.response.send_message(
                "*No songs in the library yet.*", ephemeral=True
            )
            return
        view = SongListView(songs)
        view._update_buttons()
        await interaction.response.send_message(
            embed=view._build_embed(), view=view, ephemeral=True
        )

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
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to delete songs.", ephemeral=True
            )
            return
        await interaction.response.send_modal(DeleteSongModal())

    @discord.ui.button(
        label="👍",
        style=discord.ButtonStyle.success,
        custom_id="music:like_current",
        row=2,
    )
    async def like_current(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        ok, msg = await interaction.client.submit_current_song_feedback(  # type: ignore[attr-defined]
            interaction, is_like=True
        )
        prefix = "👍" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)

    @discord.ui.button(
        label="👎",
        style=discord.ButtonStyle.secondary,
        custom_id="music:dislike_current",
        row=2,
    )
    async def dislike_current(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        ok, msg = await interaction.client.submit_current_song_feedback(  # type: ignore[attr-defined]
            interaction, is_like=False
        )
        prefix = "👎" if ok else "❌"
        await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)


class ShadyControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📋 Playlist",
        style=discord.ButtonStyle.secondary,
        custom_id="shady:song_list",
        row=0,
    )
    async def song_list(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        songs = get_all_songs()
        if not songs:
            await interaction.response.send_message(
                "*No songs in the library yet.*", ephemeral=True
            )
            return
        view = SongListView(songs)
        view._update_buttons()
        await interaction.response.send_message(
            embed=view._build_embed(), view=view, ephemeral=True
        )

    @discord.ui.button(
        label="➕ Add Song",
        style=discord.ButtonStyle.success,
        custom_id="shady:add_song",
        row=0,
    )
    async def add_song(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(AddSongModal())

    @discord.ui.button(
        label="⛔ Disable Song",
        style=discord.ButtonStyle.danger,
        custom_id="shady:disable_song",
        row=0,
    )
    async def disable_song(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not is_music_manager(interaction):
            await interaction.response.send_message(
                "❌ You need the **Music Manager** role to disable songs.", ephemeral=True
            )
            return
        await interaction.response.send_modal(DeleteSongByIdModal())

