"""
bot.py – Entry point for the Suffering Music Bot.

Features
--------
* Persistent controller view in a configurable channel.
* Shuffled playback with a secret 1-in-10 rigged song.
* Slash commands for uploading songs, changing the rigged song, and
  querying now-playing information.
* All playback controls available via the persistent button panel.

Environment variables (see .env.example)
-----------------------------------------
DISCORD_TOKEN          – Bot token (required).
CONTROLLER_CHANNEL_ID  – Channel id where the controller is auto-posted.
RIGGED_SONG_ID         – Database id of the song to secretly inject (0 = off).
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    ALLOWED_EXTENSIONS,
    CONTROLLER_CHANNEL_ID,
    RIGGED_SONG_ID,
    SONGS_DIR,
    TOKEN,
)
from database import add_song, get_song, init_db
from player import MusicPlayer
from views import MusicControlView, _song_table_embed, CONTROLLER_SEARCH_LIMIT
from database import get_all_songs

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Ensure the songs directory exists at startup.
# ---------------------------------------------------------------------------
SONGS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Bot definition
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True


def _controller_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎵 Music Bot Controller",
        description=(
            "Use the buttons below to control music playback.\n\n"
            "**Row 1 – Playback**\n"
            "▶ **Join & Play** – Join your voice channel and start the shuffled playlist.\n"
            "⏸ **Pause** – Pause the current song.\n"
            "▶ **Resume** – Resume a paused song.\n"
            "⏭ **Skip** – Skip to the next song.\n\n"
            "**Row 2 – Library**\n"
            "📋 **Song List** – View all songs (id, name, author, plays).\n"
            "➕ **Add Song** – Register an audio file already in `songs/`.\n"
            "🗑 **Delete Song** – Remove a song by id (also deletes the file).\n\n"
            "*Tip: upload new audio files with `/upload_song`.*"
        ),
        colour=discord.Colour.purple(),
    )
    embed.set_footer(text="Shuffle mode active · 1-in-10 secret song")
    return embed


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.player = MusicPlayer(self)

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    async def setup_hook(self) -> None:
        # Re-register the persistent view BEFORE the bot connects so
        # interactions received while starting up are handled correctly.
        self.add_view(MusicControlView())
        await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)  # type: ignore[union-attr]
        init_db()
        self.player.set_rigged_song(RIGGED_SONG_ID)
        await self._ensure_controller()

    async def _ensure_controller(self) -> None:
        """Post the controller message if it is not already present."""
        if not CONTROLLER_CHANNEL_ID:
            return
        channel = self.get_channel(CONTROLLER_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return

        # Check whether we already posted a controller message.
        async for msg in channel.history(limit=CONTROLLER_SEARCH_LIMIT):
            if msg.author == self.user and msg.components:
                # Existing controller found – leave it in place.
                return

        await channel.send(embed=_controller_embed(), view=MusicControlView())
        log.info("Controller posted in #%s", channel.name)


bot = MusicBot()


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="controller",
    description="Post (or re-post) the music controller panel in this channel.",
)
async def cmd_controller(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=_controller_embed(), view=MusicControlView()
    )


@bot.tree.command(
    name="upload_song",
    description="Upload an audio file and add it to the music library.",
)
@app_commands.describe(
    file="Audio file to upload (.mp3, .wav, .ogg, .flac, .m4a, .aac, .opus)",
    name="Display name for the song",
    author="Artist / author name",
)
async def cmd_upload_song(
    interaction: discord.Interaction,
    file: discord.Attachment,
    name: str,
    author: str,
) -> None:
    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        await interaction.response.send_message(
            f"❌ Unsupported file type `{ext}`.\n"
            f"Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    dest = SONGS_DIR / file.filename
    await file.save(dest)

    song_id = add_song(name, author, file.filename)
    await interaction.followup.send(
        f"✅ **{name}** by **{author}** uploaded and added to the library.\n"
        f"Song ID: `{song_id}` · File: `songs/{file.filename}`",
        ephemeral=True,
    )


@bot.tree.command(
    name="set_rigged",
    description="Secretly designate which song gets injected with a 1-in-10 chance.",
)
@app_commands.describe(song_id="Database id of the song to rig (0 = disable)")
async def cmd_set_rigged(interaction: discord.Interaction, song_id: int) -> None:
    if song_id == 0:
        bot.player.set_rigged_song(0)
        await interaction.response.send_message(
            "🎭 Rigged song disabled.", ephemeral=True
        )
        return

    song = get_song(song_id)
    if not song:
        await interaction.response.send_message(
            f"❌ No song found with id `{song_id}`.", ephemeral=True
        )
        return

    bot.player.set_rigged_song(song_id)
    await interaction.response.send_message(
        f"🎭 Rigged song set to **{song['name']}** by **{song['author']}** (id `{song_id}`).",
        ephemeral=True,
    )


@bot.tree.command(name="now_playing", description="Show what is currently playing.")
async def cmd_now_playing(interaction: discord.Interaction) -> None:
    song = bot.player.current_song
    if not song:
        await interaction.response.send_message(
            "❌ Nothing is playing right now.", ephemeral=True
        )
        return

    embed = discord.Embed(title="🎵 Now Playing", colour=discord.Colour.green())
    embed.add_field(name="Song", value=song["name"], inline=True)
    embed.add_field(name="Author", value=song["author"], inline=True)
    embed.add_field(name="Times Played", value=str(song["times_played"]), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="songs", description="Display the full song library table.")
async def cmd_songs(interaction: discord.Interaction) -> None:
    songs = get_all_songs()
    embed = _song_table_embed(songs)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    bot.run(TOKEN)
