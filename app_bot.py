from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
import re
from pathlib import Path

import discord
from discord.ext import commands

from broadcast import DAYS as DJ_DAYS, DJEventScheduler
from config import (
    COLLECTOR_AVATAR_PATH,
    COLLECTOR_BANNER_PATH,
    COLLECTOR_BOT_NAME,
    CONTROLLER_CHANNEL_ID,
    CONTROLLER_STATE_PATH,
    DJ_EVENTS_DIR,
    HEAVEN_AVATAR_PATH,
    HEAVEN_BANNER_PATH,
    HEAVEN_BOT_NAME,
    QUOTES_CHANNEL_ID,
    RIGGED_SONG_IDS,
    SONGS_DIR,
    SUFFERING_AVATAR_PATH,
    SUFFERING_BANNER_PATH,
    SUFFERING_BOT_NAME,
)
from database import (
    apply_song_feedback,
    deactivate_song,
    get_song,
    hard_delete_song,
    init_db,
    sync_ads_from_disk,
)
from player import MusicPlayer
from views import (
    CONTROLLER_SEARCH_LIMIT,
    REACT_DEACTIVATE,
    REACT_HARD_DELETE,
    MusicControlView,
    ShadyControlView,
    _resolve_username,
)

log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

_SUFFERING_LINES: tuple[str, ...] = (
    "💀 | || || |_",
    "🛒 Hey, you. You're finally awake.",
    "🎁 https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "📺 This episode is called: suffering.",
    "🍽️ i've really cooked with this one! Bone ape le feet!",
    "🧟‍♂️ The suffering will never end. The suffering will never nd."
    "The suffering will nevr nd. The suffering will nvr nd."
    "The suffring will nvr nd. Th suffring will nvr nd."
    "   This pain will not stop. REEEEEEEEEEEEEEEEEEEEEEEEE.",
    "🎶 Now playing: the sweet sound of forgiveness, screaming then silence.",
    "🚫I'm not racist, I was just sayin that all NIG\nG\nG\nG\nG\nG\n"
    "**BOT HAS BEEN CENSORED FOR RACISM**\n**PLEASE REFORM BOT**\n"
    "*For legal reasons, the views and opinions expressed by the bot "
    "are not reflective of the developers or the server community, "
    "and are solely intended for entertainment purposes. "
    "The bot will be severly disciplined with a single punitive "
    "virtual slap on the wrist the next time it expresses these views again.",
    "quoteable_moment",
    "quoteable_moment",
    "quoteable_moment",
    "quoteable_moment",
)


class BrandingRateLimitError(RuntimeError):
    """Raised when Discord profile edits are rate-limited."""


def _controller_embed(
    song: dict | None = None,
    label: str | None = None,
    added_by: str | None = None,
    title: str = SUFFERING_BOT_NAME,
    is_paused: bool = False,
    colour: discord.Colour | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=f"🎵 {title}", colour=colour or discord.Colour.purple())
    status = "⏸ Paused" if is_paused else "▶ Playing"
    if song:
        embed.add_field(
            name=status,
            value=f"**{song['name']}** — **{song['artist']}**",
            inline=False,
        )
        embed.add_field(name="Added by", value=added_by or "Unknown", inline=False)
    elif label:
        label_map = {
            "ad": "📢 Advertisement",
            "dj intro": "🎤 DJ Intro",
            "dj outro": "🎤 DJ Outro",
            "dj event": "🎤 DJ Event",
            "intermission": "📢 Intermission",
        }
        display = label_map.get(label.lower(), f"🎵 {label.title()}")
        embed.add_field(name=status, value=display, inline=False)
    else:
        embed.description = "*Not currently playing. Use ▶ Play to start.*"
    return embed


def _shady_controller_embed(colour: discord.Colour | None = None) -> discord.Embed:
    return discord.Embed(
        title=f"🕶️ {COLLECTOR_BOT_NAME}",
        description=(
            "Drop songs here and keep the stash growing.\n"
            "Use **➕ Add Song** to feed the vault, **📋 Playlist** to inspect the haul, "
            "and **⛔ Disable Song** to bury tracks."
        ),
        colour=colour or discord.Colour.purple(),
    )


def _help_tutorial_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎧 SufferingFM Quick Start",
        description="Mini guide for the controller buttons and slash commands.",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="1) Start playback",
        value=(
            "Join a voice channel, then press **▶ Play** on the controller.\n"
            "Use **⏸ Pause**, **⏭ Skip**, and **📞 Leave** as needed."
        ),
        inline=False,
    )
    embed.add_field(
        name="2) Browse songs",
        value=(
            "Use **📋 Playlist** on the controller for the active song list.\n"
            "Slash commands: **`/songs`**, **`/songs_all`** (Manager), **`/search`**."
        ),
        inline=False,
    )
    embed.add_field(
        name="3) Check the current track / send feedback",
        value=(
            "Use **`/now_playing`** to see the current song.\n"
            "Send feedback with **👍 / 👎** buttons or **`/like`** / **`/dislike`**."
        ),
        inline=False,
    )
    embed.add_field(
        name="4) Add music",
        value=(
            "Music Managers can use **`/upload_song`** for attachments or YouTube links.\n"
            "The **➕ Add Song** button can add a song directly from a YouTube link."
        ),
        inline=False,
    )
    embed.add_field(
        name="5) Manager tools",
        value=(
            "**`/toggle_song`**, **`/toggle_song_id`**, **`/delete_song_id`**,\n"
            "**`/set_rigged_pool`**, **`/play_dj_event`**."
        ),
        inline=False,
    )
    embed.set_footer(text="Some commands/buttons require the Music Manager role.")
    return embed


def _read_optional_bytes(path: str) -> bytes | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    return p.read_bytes()


def _unique_path(directory: Path, filename: str) -> Path:
    safe = Path(filename).name
    base = Path(safe).stem or "audio"
    ext = Path(safe).suffix.lower()
    candidate = directory / f"{base}{ext}"
    n = 1
    while candidate.exists():
        candidate = directory / f"{base}_{n}{ext}"
        n += 1
    return candidate


def _infer_target(source: str | None, selected: str | None) -> str:
    if selected in {"song", "ad"}:
        return selected
    if source:
        m = re.search(r"\b(?:target|type|kind)\s*:\s*(song|ad)\b", source, flags=re.IGNORECASE)
        if m:
            return m.group(1).lower()
    return "song"


class MusicBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.player = MusicPlayer(self)
        self.pending_deletes: dict[int, dict] = {}
        self._controller_mode: str = "shady"
        self._persona_mode: str = "collector"
        self._branding_mode: str | None = None
        self._branding_task: asyncio.Task | None = None
        self.controller_message: discord.Message | None = None
        self._controller_song: dict | None = None
        self._controller_label: str | None = None
        self._purge_code: str | None = None
        self._purge_code_expiry: float = 0.0
        self._startup_state_announcement_sent: bool = False

    async def setup_hook(self) -> None:
        self.add_view(MusicControlView())
        self.add_view(ShadyControlView())
        await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)  # type: ignore[union-attr]
        init_db()
        sync_ads_from_disk()
        self.player.set_rigged_songs(list(RIGGED_SONG_IDS))
        self._load_controller_state()
        self._sync_controller_mode_from_persona_mode()

        DJ_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        for day in DJ_DAYS:
            (DJ_EVENTS_DIR / day).mkdir(parents=True, exist_ok=True)
        self.player.set_dj_events(DJEventScheduler(DJ_EVENTS_DIR))

        try:
            await self._apply_branding_for_day()
        except Exception as exc:
            log.warning("Initial branding apply failed: %s", exc)
        if self._branding_task is None or self._branding_task.done():
            self._branding_task = asyncio.create_task(self._branding_loop())

        self.player.on_track_start = self.update_controller_now_playing
        await self._ensure_controller()
        if not self._startup_state_announcement_sent:
            await self.announce_state(channel=None)
            self._startup_state_announcement_sent = True

    async def _ensure_controller(self) -> None:
        if not CONTROLLER_CHANNEL_ID:
            return
        channel = self.get_channel(CONTROLLER_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return
        stale_controllers: list[discord.Message] = []
        async for msg in channel.history(limit=CONTROLLER_SEARCH_LIMIT):
            if msg.author == self.user and msg.components:
                stale_controllers.append(msg)
        for stale in stale_controllers:
            try:
                await stale.delete()
            except Exception as exc:
                log.warning("Failed to delete old controller message %s: %s", stale.id, exc)
        msg = await channel.send(
            embed=self._active_controller_embed(),
            view=self._active_controller_view(),
        )
        self.controller_message = msg
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
        if self._persona_mode == "forced_heaven":
            target = "heaven"
        elif self._persona_mode == "collector":
            target = "collector"
        else:
            weekday = datetime.datetime.now(datetime.timezone.utc).weekday()
            target = "heaven" if weekday == 6 else "suffering"
        if target == self._branding_mode:
            return

        banner = None
        if target == "heaven":
            username = HEAVEN_BOT_NAME
            avatar = _read_optional_bytes(HEAVEN_AVATAR_PATH)
            banner = _read_optional_bytes(HEAVEN_BANNER_PATH)
        elif target == "collector":
            username = COLLECTOR_BOT_NAME
            avatar = _read_optional_bytes(COLLECTOR_AVATAR_PATH)
            banner = _read_optional_bytes(COLLECTOR_BANNER_PATH)
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
            await self._update_guild_nicknames(username)
            log.info("Branding switched to %s mode.", target)
        except discord.HTTPException as exc:
            if "too fast" in str(exc).lower():
                raise BrandingRateLimitError(str(exc)) from exc
            log.warning("Could not apply %s branding: %s", target, exc)
        except Exception as exc:
            log.warning("Could not apply %s branding: %s", target, exc)

    async def _update_guild_nicknames(self, nickname: str) -> None:
        if self.user is None:
            return
        for guild in self.guilds:
            member = guild.get_member(self.user.id)
            if member is None:
                continue
            if member.nick == nickname:
                continue
            try:
                await member.edit(nick=nickname)
            except Exception as exc:
                log.debug("Could not set nickname in guild %s: %s", guild.id, exc)

    def _current_brand_name(self) -> str:
        if self._branding_mode == "heaven":
            return HEAVEN_BOT_NAME
        if self._branding_mode == "collector":
            return COLLECTOR_BOT_NAME
        return SUFFERING_BOT_NAME

    def _sync_controller_mode_from_persona_mode(self) -> None:
        self._controller_mode = "shady" if self._persona_mode == "collector" else "full"

    def _load_controller_state(self) -> None:
        try:
            if not CONTROLLER_STATE_PATH.exists():
                return
            raw = json.loads(CONTROLLER_STATE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to load controller state: %s", exc)
            return
        mode = raw.get("persona_mode")
        if mode in {"collector", "day_cycle", "forced_heaven"}:
            self._persona_mode = mode

    def _save_controller_state(self) -> None:
        payload = {"persona_mode": self._persona_mode}
        try:
            CONTROLLER_STATE_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("Failed to save controller state: %s", exc)

    def _active_controller_view(self) -> discord.ui.View:
        if self._controller_mode == "shady":
            return ShadyControlView()
        return MusicControlView()

    def _active_controller_embed(self) -> discord.Embed:
        colour = self._state_colour()
        if self._controller_mode == "shady":
            return _shady_controller_embed(colour=colour)
        if self._controller_song:
            return _controller_embed(
                song=self._controller_song,
                added_by=None,
                title=self._current_brand_name(),
                is_paused=self.player.is_paused(),
                colour=colour,
            )
        if self._controller_label:
            return _controller_embed(
                label=self._controller_label,
                title=self._current_brand_name(),
                is_paused=self.player.is_paused(),
                colour=colour,
            )
        return _controller_embed(title=self._current_brand_name(), colour=colour)

    def _state_key(self) -> str:
        if self._persona_mode == "forced_heaven":
            return "heaven"
        if self._persona_mode == "collector":
            return "collector"
        weekday = datetime.datetime.now(datetime.timezone.utc).weekday()
        return "heaven" if weekday == 6 else "suffering"

    def _state_colour(self) -> discord.Colour:
        key = self._state_key()
        if key == "collector":
            return discord.Colour.purple()
        if key == "heaven":
            return discord.Colour.from_rgb(255, 215, 0)
        return discord.Colour.from_rgb(0, 0, 0)

    async def _switch_persona_with_branding(self, persona_mode: str) -> bool:
        previous_persona = self._persona_mode
        self._persona_mode = persona_mode
        self._sync_controller_mode_from_persona_mode()
        self._save_controller_state()
        try:
            await self._apply_branding_for_day()
        except BrandingRateLimitError:
            self._persona_mode = previous_persona
            self._sync_controller_mode_from_persona_mode()
            self._save_controller_state()
            try:
                await self._apply_branding_for_day()
            except Exception as exc:
                log.warning("Failed to restore prior branding after rate limit: %s", exc)
            await self.refresh_controller_status()
            return False
        await self.refresh_controller_status()
        return True

    async def _state_announcement(self) -> str:
        key = self._state_key()
        if key == "collector":
            return "🕶️ The Collector has come to collect."
        if key == "heaven":
            return "✨ Heaven's gates have been thrown open."
        announcement = random.choice(_SUFFERING_LINES)
        if announcement != "quoteable_moment":
            return announcement
        quote = random.choice([
            m.content async for m in self.get_channel(QUOTES_CHANNEL_ID).history(limit=10000)
            if len(m.content) >= 2
        ])
        return f"You can't run from a past that haunts you...\n\n{quote}"

    async def announce_state(self, channel: discord.abc.Messageable | None) -> None:
        target_channel = channel
        if target_channel is None:
            target_channel = self.controller_message.channel if self.controller_message else None
        if target_channel is None:
            return
        await target_channel.send(await self._state_announcement())

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
        return True, msg

    async def refresh_controller_status(self) -> None:
        if self.controller_message is None:
            return
        colour = self._state_colour()
        if self._controller_mode == "shady":
            embed = _shady_controller_embed(colour=colour)
        else:
            guild = self.controller_message.guild
            if self._controller_song:
                added_by = await _resolve_username(
                    self, self._controller_song.get("added_by", ""), guild
                )
                embed = _controller_embed(
                    song=self._controller_song,
                    added_by=added_by,
                    title=self._current_brand_name(),
                    is_paused=self.player.is_paused(),
                    colour=colour,
                )
            elif self._controller_label:
                embed = _controller_embed(
                    label=self._controller_label,
                    title=self._current_brand_name(),
                    is_paused=self.player.is_paused(),
                    colour=colour,
                )
            else:
                embed = _controller_embed(
                    title=self._current_brand_name(),
                    colour=colour,
                )
        try:
            await self.controller_message.edit(
                embed=embed,
                view=self._active_controller_view(),
            )
        except Exception as exc:
            log.warning("Failed to update controller embed: %s", exc)

    async def clear_controller_now_playing(self) -> None:
        self._controller_song = None
        self._controller_label = None
        await self.refresh_controller_status()

    async def update_controller_now_playing(
        self, song: dict | None, label: str
    ) -> None:
        if song:
            self._controller_song = song
            self._controller_label = None
        else:
            self._controller_song = None
            self._controller_label = label
        await self.refresh_controller_status()

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


def create_bot() -> MusicBot:
    return MusicBot()
