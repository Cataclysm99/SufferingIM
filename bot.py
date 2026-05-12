"""
bot.py – Entry point for the SufferingFM bot.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from broadcast import DAYS as DJ_DAYS, DJEventScheduler
from config import (
    ALLOWED_EXTENSIONS,
    CONTROLLER_CHANNEL_ID,
    DJ_EVENTS_DIR,
    HEAVEN_AVATAR_PATH,
    HEAVEN_BANNER_PATH,
    HEAVEN_BOT_NAME,
    RIGGED_SONG_IDS,
    SONGS_DIR,
    SUFFERING_AVATAR_PATH,
    SUFFERING_BANNER_PATH,
    SUFFERING_BOT_NAME,
    TOKEN,
)
from database import (
    activate_song,
    add_song,
    apply_song_feedback,
    deactivate_song,
    get_all_songs,
    get_all_songs_admin,
    get_song,
    get_songs_by_name,
    hard_delete_song,
    init_db,
    search_songs,
    sync_ads_from_disk,
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

SONGS_DIR.mkdir(parents=True, exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True


def _controller_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎵 SufferingFM Controller",
        description=(
            "Use buttons below to control playback.\n\n"
            "**Row 1 – Playback**: Play/Resume · Pause · Skip · Leave\n"
            "**Row 2 – Library**: Playlist · Full Library · Add Song · Delete Song\n"
            "**Row 3 – Feedback**: 👍 · 👎\n\n"
            "Intermissions: ads (~30 min) and DJ events (~60 min).\n"
            "DJ cycle: intro at start, hourly random events, outro around 6h, then restart."
        ),
        colour=discord.Colour.purple(),
    )
    return embed


def _read_optional_bytes(path: str) -> bytes | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    return p.read_bytes()


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.player = MusicPlayer(self)
        self.pending_deletes: dict[int, dict] = {}
        self._branding_mode: str | None = None
        self._branding_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        self.add_view(MusicControlView())
        await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)  # type: ignore[union-attr]
        init_db()
        sync_ads_from_disk()
        self.player.set_rigged_songs(list(RIGGED_SONG_IDS))

        DJ_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        for day in DJ_DAYS:
            (DJ_EVENTS_DIR / day).mkdir(parents=True, exist_ok=True)
        self.player.set_dj_events(DJEventScheduler(DJ_EVENTS_DIR))

        await self._apply_branding_for_day()
        if self._branding_task is None or self._branding_task.done():
            self._branding_task = asyncio.create_task(self._branding_loop())

        await self._ensure_controller()

    async def _ensure_controller(self) -> None:
        if not CONTROLLER_CHANNEL_ID:
            return
        channel = self.get_channel(CONTROLLER_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return
        async for msg in channel.history(limit=CONTROLLER_SEARCH_LIMIT):
            if msg.author == self.user and msg.components:
                return
        await channel.send(embed=_controller_embed(), view=MusicControlView())
        log.info("Controller posted in #%s", channel.name)

    async def _branding_loop(self) -> None:
        while True:
            try:
                await self._apply_branding_for_day()
            except Exception as exc:
                log.warning("Branding check failed: %s", exc)
            await asyncio.sleep(300)

    async def _apply_branding_for_day(self) -> None:
        if self.user is None:
            return
        # Monday=0, Sunday=6; Sunday triggers heaven mode.
        weekday = datetime.datetime.now(datetime.UTC).weekday()
        target = "heaven" if weekday == 6 else "suffering"
        if target == self._branding_mode:
            return

        if target == "heaven":
            username = HEAVEN_BOT_NAME
            avatar = _read_optional_bytes(HEAVEN_AVATAR_PATH)
            banner = _read_optional_bytes(HEAVEN_BANNER_PATH)
        else:
            username = SUFFERING_BOT_NAME
            avatar = _read_optional_bytes(SUFFERING_AVATAR_PATH)
            banner = _read_optional_bytes(SUFFERING_BANNER_PATH)

        kwargs: dict = {"username": username}
        if avatar is not None:
            kwargs["avatar"] = avatar
        if banner is not None:
            kwargs["banner"] = banner

        try:
            await self.user.edit(**kwargs)
            self._branding_mode = target
            log.info("Branding switched to %s mode.", target)
        except Exception as exc:
            log.warning("Could not apply %s branding: %s", target, exc)

    async def submit_current_song_feedback(
        self, interaction: discord.Interaction, is_like: bool
    ) -> tuple[bool, str]:
        song = self.player.current_song
        if not song:
            return False, "Nothing is currently playing."
        ok, msg = apply_song_feedback(song["id"], interaction.user.id, is_like)
        if not ok:
            return False, msg
        fresh = get_song(song["id"])
        if fresh:
            self.player.current_song = fresh
        return True, "Feedback received."

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if self.user and payload.user_id == self.user.id:
            return

        pending = self.pending_deletes.get(payload.message_id)
        if not pending:
            return
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
                    f"⛔ **{song['name']}** by **{song['artist']}** has been deactivated.\n"
                    "Use `/toggle_song` or `/toggle_song_id` to re-enable it."
                )
            )
            await msg.clear_reactions()
            del self.pending_deletes[payload.message_id]
        elif emoji == REACT_HARD_DELETE:
            hard_delete_song(song["id"])
            song_path = SONGS_DIR / song["filename"]
            if song_path.exists():
                song_path.unlink()
            await msg.edit(
                content=(
                    f"🗑️ **{song['name']}** by **{song['artist']}** has been permanently deleted."
                )
            )
            await msg.clear_reactions()
            del self.pending_deletes[payload.message_id]


bot = MusicBot()


@bot.tree.command(name="controller", description="Post the controller panel.")
async def cmd_controller(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=_controller_embed(), view=MusicControlView())


@bot.tree.command(name="upload_song", description="Upload an audio file and add it to the library.")
@app_commands.describe(
    file="Audio file to upload",
    name="Display name",
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
            f"❌ Unsupported file type `{ext}`.\nAllowed: {', '.join(ALLOWED_EXTENSIONS)}",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    dest = SONGS_DIR / file.filename
    await file.save(dest)
    song_id = add_song(name, artist, file.filename, str(interaction.user.id))
    await interaction.followup.send(
        f"✅ Added **{name}** by **{artist}**.\nSong ID: `{song_id}`",
        ephemeral=True,
    )


@bot.tree.command(name="search", description="Search songs by name, artist, uploader, or id.")
@app_commands.describe(field="Field", query="Search term")
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
        await interaction.response.send_message("No matching songs found.", ephemeral=True)
        return
    embed = _song_table_embed(results, title=f'🔎 Results: {field.name} = "{query}"')
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="toggle_song", description="Activate/deactivate a song by name.")
@app_commands.describe(name="Song name")
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
            f"Multiple songs named **{name}** were found. Use `/toggle_song_id`.",
            embed=embed,
            ephemeral=True,
        )
        return
    song = matches[0]
    if song.get("available", 1):
        deactivate_song(song["id"])
        await interaction.response.send_message(f"⛔ Deactivated **{song['name']}**.", ephemeral=True)
    else:
        activate_song(song["id"])
        await interaction.response.send_message(f"✅ Re-activated **{song['name']}**.", ephemeral=True)


@bot.tree.command(name="toggle_song_id", description="Activate/deactivate by song id.")
@app_commands.describe(song_id="Song ID")
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
        await interaction.response.send_message(f"⛔ Deactivated **{song['name']}**.", ephemeral=True)
    else:
        activate_song(song_id)
        await interaction.response.send_message(f"✅ Re-activated **{song['name']}**.", ephemeral=True)


@bot.tree.command(name="delete_song_id", description="Begin two-step delete by song id.")
@app_commands.describe(song_id="Song ID")
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
    await interaction.response.send_message(msg_content)
    msg = await interaction.original_response()
    await msg.add_reaction(REACT_DEACTIVATE)
    await msg.add_reaction(REACT_HARD_DELETE)
    bot.pending_deletes[msg.id] = {"user_id": interaction.user.id, "song": song}


@bot.tree.command(name="songs", description="Show active songs.")
async def cmd_songs(interaction: discord.Interaction) -> None:
    embed = _song_table_embed(get_all_songs())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="songs_all", description="Show all songs including deactivated.")
async def cmd_songs_all(interaction: discord.Interaction) -> None:
    embed = _song_table_embed(get_all_songs_admin(), title="🎵 Song Library (All)")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="set_rigged_pool",
    description="Set rigged song IDs as comma-separated list (empty/0 to clear).",
)
@app_commands.describe(song_ids="Example: 3,7,12")
async def cmd_set_rigged_pool(interaction: discord.Interaction, song_ids: str) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role.", ephemeral=True
        )
        return
    raw = [p.strip() for p in song_ids.split(",")]
    ids: list[int] = []
    for p in raw:
        if not p or p == "0":
            continue
        try:
            ids.append(int(p))
        except ValueError:
            continue
    bot.player.set_rigged_songs(ids)
    if not ids:
        await interaction.response.send_message("🎭 Rigged song pool cleared.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"🎭 Rigged song pool set to IDs: {', '.join(str(i) for i in ids)}",
            ephemeral=True,
        )


@bot.tree.command(name="now_playing", description="Show current song.")
async def cmd_now_playing(interaction: discord.Interaction) -> None:
    song = bot.player.current_song
    if not song:
        await interaction.response.send_message("❌ Nothing is playing right now.", ephemeral=True)
        return
    embed = discord.Embed(title="🎵 Now Playing", colour=discord.Colour.green())
    embed.add_field(name="Song", value=song["name"], inline=True)
    embed.add_field(name="Artist", value=song["artist"], inline=True)
    embed.add_field(name="Plays", value=str(song.get("times_played", 0)), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="play_dj_event", description="Play a random DJ event clip now.")
@app_commands.describe(day="Optional day abbreviation: MON TUE WED THU FRI SAT SUN")
async def cmd_play_dj_event(
    interaction: discord.Interaction,
    day: str | None = None,
) -> None:
    if not is_music_manager(interaction):
        await interaction.response.send_message(
            "❌ You need the **Music Manager** role to trigger DJ events.", ephemeral=True
        )
        return
    if not bot.player.is_connected():
        await interaction.response.send_message(
            "❌ Bot is not connected to voice. Press Play/Resume first.", ephemeral=True
        )
        return
    target_day = None
    if day:
        day_map = {
            "MON": "monday", "TUE": "tuesday", "WED": "wednesday",
            "THU": "thursday", "FRI": "friday", "SAT": "saturday", "SUN": "sunday",
        }
        target_day = day_map.get(day.upper())
        if target_day is None:
            await interaction.response.send_message(
                "❌ Invalid day. Use MON TUE WED THU FRI SAT SUN.", ephemeral=True
            )
            return
    started = await bot.player.play_dj_event_now(day=target_day)
    if started:
        await interaction.response.send_message("🎙️ Playing DJ event now.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ No DJ event clip available.", ephemeral=True)


@bot.tree.command(name="like", description="Send a like for the current song.")
async def cmd_like(interaction: discord.Interaction) -> None:
    ok, msg = await bot.submit_current_song_feedback(interaction, is_like=True)
    prefix = "👍" if ok else "❌"
    await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)


@bot.tree.command(name="dislike", description="Send a dislike for the current song.")
async def cmd_dislike(interaction: discord.Interaction) -> None:
    ok, msg = await bot.submit_current_song_feedback(interaction, is_like=False)
    prefix = "👎" if ok else "❌"
    await interaction.response.send_message(f"{prefix} {msg}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)
