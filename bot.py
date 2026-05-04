"""
bot.py – Entry point for the Suffering Music Bot.

Features
--------
* Persistent controller view in a configurable channel.
* Shuffled playback with a secret 1-in-10 rigged song.
* Two-step song deletion via emoji reactions (deactivate or hard-delete).
* Slash commands for uploading songs, searching, toggling availability,
  changing the rigged song, and querying now-playing information.

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
from database import (
    activate_song,
    add_song,
    deactivate_song,
    get_all_songs,
    get_all_songs_admin,
    get_song,
    get_songs_by_name,
    hard_delete_song,
    init_db,
    search_songs,
)
from player import MusicPlayer
from views import (
    CONTROLLER_SEARCH_LIMIT,
    REACT_DEACTIVATE,
    REACT_HARD_DELETE,
    MusicControlView,
    _build_delete_confirm_message,
    _song_table_embed,
    is_music_manager,
)

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
            "▶ **Play / Resume** – Join your voice channel and play, or resume if paused.\n"
            "⏸ **Pause** – Pause the current song.\n"
            "⏭ **Skip** – Skip to the next song.\n"
            "📞 **Leave** – Disconnect the bot from the voice channel.\n\n"
            "**Row 2 – Library**\n"
            "📋 **Playlist** – View currently active (available) songs.\n"
            "📚 **Full Library** – View all songs, including deactivated ones.\n"
            "➕ **Add Song** – *(Music Manager only)* Register a file already in `songs/`.\n"
            "🗑 **Delete Song** – *(Music Manager only)* Begin the two-step delete process.\n\n"
            "*Tip: upload new audio files with `/upload_song`.*\n"
            "*Use `/search` to find songs by name, artist, uploader, or id.*"
        ),
        colour=discord.Colour.purple(),
    )
    embed.set_footer(text="Shuffle mode active · 1-in-10 secret song")
    return embed


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.player = MusicPlayer(self)
        # Tracks pending two-step deletes: {message_id: {"user_id": int, "song": dict}}
        self.pending_deletes: dict[int, dict] = {}

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

    # ------------------------------------------------------------------
    # Reaction-based delete confirmation
    # ------------------------------------------------------------------

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        """Handle ✅ / 🗑️ reactions for the two-step song delete flow."""
        # Ignore the bot's own reactions.
        if self.user and payload.user_id == self.user.id:
            return

        pending = self.pending_deletes.get(payload.message_id)
        if not pending:
            return

        # Only the user who triggered the delete can confirm it.
        if payload.user_id != pending["user_id"]:
            return

        emoji = str(payload.emoji)
        song = pending["song"]

        channel = self.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        msg = await channel.fetch_message(payload.message_id)

        if emoji == REACT_DEACTIVATE:
            deactivate_song(song["id"])
            await msg.edit(
                content=(
                    f"⛔ **{song['name']}** by **{song['artist']}** has been deactivated "
                    f"and will no longer play. The audio file has been kept.\n"
                    f"Use `/toggle_song` or `/toggle_song_id` to re-enable it."
                )
            )
            await msg.clear_reactions()
            del self.pending_deletes[payload.message_id]
            log.info("Song %d deactivated by user %d", song["id"], payload.user_id)

        elif emoji == REACT_HARD_DELETE:
            hard_delete_song(song["id"])
            song_path = SONGS_DIR / song["filename"]
            if song_path.exists():
                song_path.unlink()
            await msg.edit(
                content=(
                    f"🗑️ **{song['name']}** by **{song['artist']}** has been permanently "
                    f"deleted and its audio file has been removed."
                )
            )
            await msg.clear_reactions()
            del self.pending_deletes[payload.message_id]
            log.info(
                "Song %d hard-deleted by user %d", song["id"], payload.user_id
            )


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
    artist="Artist name",
)
async def cmd_upload_song(
    interaction: discord.Interaction,
    file: discord.Attachment,
    name: str,
    artist: str,
) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to upload songs.", ephemeral=True
        )
        return

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

    added_by = str(interaction.user.id)
    song_id = add_song(name, artist, file.filename, added_by)
    await interaction.followup.send(
        f"✅ **{name}** by **{artist}** uploaded and added to the library.\n"
        f"Song ID: `{song_id}` · File: `songs/{file.filename}`",
        ephemeral=True,
    )


@bot.tree.command(
    name="search",
    description="Search the song library by name, artist, uploader, or id.",
)
@app_commands.describe(
    field="Field to search by",
    query="Search term",
)
@app_commands.choices(field=[
    app_commands.Choice(name="Name", value="name"),
    app_commands.Choice(name="Artist", value="artist"),
    app_commands.Choice(name="Added By (user ID)", value="added_by"),
    app_commands.Choice(name="ID", value="id"),
])
async def cmd_search(
    interaction: discord.Interaction,
    field: app_commands.Choice[str],
    query: str,
) -> None:
    results = search_songs(field.value, query)
    if not results:
        if field.value == "artist":
            msg = f"Sorry, there is no artist named {query}."
        elif field.value == "name":
            msg = f"Sorry, there is no song named {query}."
        elif field.value == "added_by":
            msg = f"Sorry, there are no songs added by user {query}."
        else:
            msg = f"Sorry, there is no song with ID {query}."
        await interaction.response.send_message(msg, ephemeral=True)
        return

    embed = _song_table_embed(
        results, title=f'🔎 Results: {field.name} = "{query}"'
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="toggle_song",
    description="Activate or deactivate a song by name (case-insensitive).",
)
@app_commands.describe(name="Song name to toggle")
async def cmd_toggle_song(interaction: discord.Interaction, name: str) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to toggle songs.", ephemeral=True
        )
        return

    matches = get_songs_by_name(name)
    if not matches:
        await interaction.response.send_message(
            f"Sorry, there is no song named {name}.", ephemeral=True
        )
        return

    if len(matches) > 1:
        embed = _song_table_embed(matches, title="🔎 Multiple Matches")
        await interaction.response.send_message(
            f"Multiple songs named **{name}** were found. "
            "Use `/toggle_song_id` with the specific song ID instead.",
            embed=embed,
            ephemeral=True,
        )
        return

    song = matches[0]
    if song.get("available", 1):
        deactivate_song(song["id"])
        await interaction.response.send_message(
            f"⛔ **{song['name']}** by **{song['artist']}** has been deactivated.",
            ephemeral=True,
        )
    else:
        activate_song(song["id"])
        await interaction.response.send_message(
            f"✅ **{song['name']}** by **{song['artist']}** has been re-activated.",
            ephemeral=True,
        )


@bot.tree.command(
    name="toggle_song_id",
    description="Activate or deactivate a song by its unique ID.",
)
@app_commands.describe(song_id="The unique song ID")
async def cmd_toggle_song_id(interaction: discord.Interaction, song_id: int) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to toggle songs.", ephemeral=True
        )
        return

    song = get_song(song_id)
    if not song:
        await interaction.response.send_message(
            f"Sorry, there is no song with ID {song_id}.", ephemeral=True
        )
        return

    if song.get("available", 1):
        deactivate_song(song_id)
        await interaction.response.send_message(
            f"⛔ **{song['name']}** by **{song['artist']}** has been deactivated.",
            ephemeral=True,
        )
    else:
        activate_song(song_id)
        await interaction.response.send_message(
            f"✅ **{song['name']}** by **{song['artist']}** has been re-activated.",
            ephemeral=True,
        )


@bot.tree.command(
    name="delete_song_id",
    description="Begin the two-step delete confirmation for a song by its unique ID.",
)
@app_commands.describe(song_id="The unique song ID shown in the song list")
async def cmd_delete_song_id(interaction: discord.Interaction, song_id: int) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to delete songs.", ephemeral=True
        )
        return

    song = get_song(song_id)
    if not song:
        await interaction.response.send_message(
            f"Sorry, there is no song with ID {song_id}.", ephemeral=True
        )
        return

    msg_content = await _build_delete_confirm_message(
        song, interaction.user.id, interaction.client, interaction.guild
    )

    # Non-ephemeral so reactions can be added.
    await interaction.response.send_message(msg_content)
    msg = await interaction.original_response()

    await msg.add_reaction(REACT_DEACTIVATE)
    await msg.add_reaction(REACT_HARD_DELETE)

    bot.pending_deletes[msg.id] = {
        "user_id": interaction.user.id,
        "song": song,
    }
    log.info(
        "Pending delete started for song %d by user %d (via /delete_song_id)",
        song_id,
        interaction.user.id,
    )


@bot.tree.command(
    name="songs",
    description="Display the full active song library.",
)
async def cmd_songs(interaction: discord.Interaction) -> None:
    songs = get_all_songs()
    embed = _song_table_embed(songs)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="songs_all",
    description="Display all songs including deactivated ones (admin view).",
)
async def cmd_songs_all(interaction: discord.Interaction) -> None:
    songs = get_all_songs_admin()
    embed = _song_table_embed(songs, title="🎵 Song Library (All)")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="set_rigged",
    description="Secretly designate which song gets injected with a 1-in-10 chance.",
)
@app_commands.describe(song_id="Database id of the song to rig (0 = disable)")
async def cmd_set_rigged(interaction: discord.Interaction, song_id: int) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to change the rigged song.",
            ephemeral=True,
        )
        return

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
        f"🎭 Rigged song set to **{song['name']}** by **{song['artist']}** (id `{song_id}`).",
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
    embed.add_field(name="Artist", value=song["artist"], inline=True)
    embed.add_field(name="Times Played", value=str(song["times_played"]), inline=True)
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
