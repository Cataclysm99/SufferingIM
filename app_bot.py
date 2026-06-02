"""Discord bot setup, controller state, branding, and playback coordination."""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import random
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Awaitable, Callable

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
    parse_genre_names,
    sync_ads_from_disk,
    sync_broadcasts_from_disk,
)
from player import MusicPlayer
from views import (
    CONTROLLER_SEARCH_LIMIT,
    REACT_CANCEL,
    REACT_DEACTIVATE,
    REACT_HARD_DELETE,
    MusicControlView,
    ShadyControlView,
    _resolve_username,
)

log = logging.getLogger(__name__)
_UPLOAD_NOTIFICATION_PREVIEW_LIMIT = 1500
_DISCORD_MESSAGE_LIMIT = 2000

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


@dataclass(frozen=True, slots=True)
class _ControllerEmbedState:
    """Snapshot of the controller embed state."""

    title: str = SUFFERING_BOT_NAME
    song: dict | None = None
    label: str | None = None
    added_by: str | None = None
    is_paused: bool = False
    colour: discord.Colour | None = None


@dataclass(slots=True)
class _UploadQueueState:
    """Async queue state for serializing upload requests."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_requests: int = 0


@dataclass(slots=True)
class _BotVisualState:
    """Mutable controller and branding state kept off the bot object itself."""

    controller_mode: str = "shady"
    persona_mode: str = "collector"
    branding_mode: str | None = None
    branding_task: asyncio.Task[None] | None = None
    controller_song: dict | None = None
    controller_label: str | None = None
    upload_queue: _UploadQueueState = field(default_factory=_UploadQueueState)


class BrandingRateLimitError(RuntimeError):
    """Raised when Discord profile edits are rate-limited."""


def _split_discord_message_lines(
    message: str,
    *,
    limit: int = _DISCORD_MESSAGE_LIMIT,
) -> list[str]:
    """Split content into Discord-safe chunks, preferring line boundaries."""
    chunks: list[str] = []
    current = ""
    for line in message.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current or not chunks:
        chunks.append(current)
    return chunks


async def send_chunked_interaction_message(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool = False,
) -> None:
    """Send long interaction content across multiple Discord messages."""
    chunks = _split_discord_message_lines(content)
    await interaction.response.send_message(chunks[0], ephemeral=ephemeral)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=ephemeral)


async def send_chunked_channel_message(
    channel: discord.abc.Messageable,
    content: str,
    **send_kwargs: object,
) -> None:
    """Send long channel content across multiple Discord messages."""
    for chunk in _split_discord_message_lines(content):
        await channel.send(chunk, **send_kwargs)


def _controller_embed(state: _ControllerEmbedState) -> discord.Embed:
    """Build the full controller embed from a single state object."""
    embed = discord.Embed(
        title=f"🎵 {state.title}",
        colour=state.colour or discord.Colour.purple(),
    )
    status = "⏸ Paused" if state.is_paused else "▶ Playing"
    if state.song:
        sponsor = state.song.get("sponsor")
        if sponsor:
            now_playing_line = f"**{state.song['name']}** — sponsored by **{sponsor}**"
        else:
            artist = state.song.get("artist", "Unknown")
            now_playing_line = f"**{state.song['name']}** — **{artist}**"
            genres = ", ".join(parse_genre_names(state.song.get("genres", "")))
            if genres:
                now_playing_line += f"\n🎼 Genres: {genres}"
        embed.add_field(
            name=status,
            value=now_playing_line,
            inline=False,
        )
        embed.add_field(name="Added by", value=state.added_by or "Unknown", inline=False)
    elif state.label:
        label_map = {
            "ad": "📢 Advertisement",
            "dj intro": "🎤 DJ Intro",
            "dj outro": "🎤 DJ Outro",
            "dj event": "🎤 DJ Event",
            "intermission": "📢 Intermission",
        }
        display = label_map.get(state.label.lower(), f"🎵 {state.label.title()}")
        embed.add_field(name=status, value=display, inline=False)
    else:
        embed.description = "*Not currently playing. Use ▶ Play to start.*"
    return embed


def _shady_controller_embed(state: _ControllerEmbedState) -> discord.Embed:
    """Build the collector-mode controller embed."""
    embed = discord.Embed(
        title=f"🕶️ {COLLECTOR_BOT_NAME}",
        description=(
            "Drop songs here and keep the stash growing.\n"
            "Use **➕ Add Song** to feed the vault, **📋 Playlist** to inspect the haul, "
            "and **⛔ Disable Song** to bury tracks."
        ),
        colour=state.colour or discord.Colour.purple(),
    )
    status = "⏸ Paused" if state.is_paused else "▶ Playing"
    if state.song:
        sponsor = state.song.get("sponsor")
        if sponsor:
            value = f"**{state.song['name']}** — sponsored by **{sponsor}**"
        else:
            value = f"**{state.song['name']}** — **{state.song.get('artist', 'Unknown')}**"
            genres = ", ".join(parse_genre_names(state.song.get("genres", "")))
            if genres:
                value += f"\n🎼 Genres: {genres}"
        embed.add_field(name=status, value=value, inline=False)
    elif state.label:
        embed.add_field(name=status, value=f"🎵 {state.label.title()}", inline=False)
    return embed


def _help_tutorial_embed() -> discord.Embed:
    """Build the help/tutorial embed sent through DMs."""
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
            "Slash commands: **`/playlist`**, **`/search`**, and **`/genres_today`**."
        ),
        inline=False,
    )
    embed.add_field(
        name="3) Current track / feedback",
        value=(
            "The controller always shows what's currently playing.\n"
            "Send feedback with **👍 / 👎** buttons or **`/like`** / **`/dislike`**."
        ),
        inline=False,
    )
    embed.add_field(
        name="4) Add music",
        value=(
            "Use the **➕ Add Song** button to submit a YouTube link directly from "
            "the controller."
        ),
        inline=False,
    )
    embed.add_field(
        name="5) More help",
        value="Managers can use **`/helpless_manager`** for advanced management docs.",
        inline=False,
    )
    embed.set_footer(text="Some features require the Music Manager role.")
    return embed


def _help_manager_tutorial_embed() -> discord.Embed:
    """Build the manager-focused help/tutorial embed for DM delivery."""
    embed = discord.Embed(
        title="🎛️ SufferingFM Manager Guide",
        description="Manager-only command reference with usage hints.",
        colour=discord.Colour.dark_teal(),
    )
    embed.add_field(
        name="Upload commands",
        value=(
            "**`/upload_song`** — add songs from attachment, YouTube links, or both.\n"
            "**`/upload_ad`** — add ad clips from attachment, YouTube links, or both.\n"
            "**`/upload_broadcast`** — add DJ clips; use `placement` as `day` or `day:slot`."
        ),
        inline=False,
    )
    embed.add_field(
        name="Library management",
        value=(
            "**`/playlist_all`** — full song list including disabled entries.\n"
            "**`/ad_list`** / **`/broadcast_list`** — view ad and DJ libraries.\n"
            "**`/toggle_song`** / **`/toggle_song_id`** — enable or disable songs.\n"
            "**`/delete_song_id`** — react-confirmed flow (✅ disable / 🗑️ delete / 🚫 cancel).\n"
            "**`/manage_songs`** — dropdown editor for song name, genres, and availability."
        ),
        inline=False,
    )
    embed.add_field(
        name="Daily genre controls",
        value=(
            "**`/enable_genres`** — enable only selected genres, unless they were "
            "previously disabled.\n"
            "**`/disable_genres`** — disable selected genres while keeping others active.\n"
            "**`/genres_today`** — show the current daily genre filter."
        ),
        inline=False,
    )
    embed.add_field(
        name="Playback + state controls",
        value=(
            "**`/set_rigged_pool`** — set weighted rigged song IDs (`3,7,12` format).\n"
            "**`/play_dj_event`** — trigger a DJ clip now (optional day override or ID).\n"
            "**`/pardon`**, **`/damn`**, **`/save`** — switch controller modes.\n"
            "**`/purge_songs`** — hard wipe songs via terminal one-time password."
        ),
        inline=False,
    )
    embed.add_field(
        name="Command rollout + troubleshooting",
        value=(
            "**`!sync`** — sync global commands and clear this server's duplicate overrides.\n"
            "If a command asks for IDs, run **`/playlist_all`** first and copy the ID column."
        ),
        inline=False,
    )
    embed.set_footer(text="Manager commands require the Music Manager role.")
    return embed


def _read_optional_bytes(path: str) -> bytes | None:
    """Read a file when it exists and a non-empty path is provided."""
    if not path:
        return None
    file_path = Path(path)
    if not file_path.is_file():
        return None
    return file_path.read_bytes()


def _unique_path(directory: Path, filename: str) -> Path:
    """Return a unique file path within a directory by appending a numeric suffix."""
    safe = Path(filename).name
    base = Path(safe).stem or "audio"
    ext = Path(safe).suffix.lower()
    candidate = directory / f"{base}{ext}"
    index = 1
    while candidate.exists():
        candidate = directory / f"{base}_{index}{ext}"
        index += 1
    return candidate


@commands.command(name="sync")
@commands.guild_only()
async def _sync_tree_command(ctx: commands.Context[MusicBot]) -> None:
    """Publish global commands and remove guild-specific command duplicates."""
    author = ctx.author
    if not isinstance(author, discord.Member) or not author.guild_permissions.manage_guild:
        await ctx.reply(
            "❌ You need the **Manage Server** permission to sync slash commands here.",
            mention_author=False,
        )
        return

    try:
        synced = await ctx.bot.tree.sync()
        ctx.bot.tree.clear_commands(guild=ctx.guild)
        cleared_guild = await ctx.bot.tree.sync(guild=ctx.guild)
    except discord.DiscordException as exc:
        log.warning(
            "Slash-command sync failed for %s (%s): %s",
            ctx.guild,
            getattr(ctx.guild, "id", "unknown"),
            exc,
        )
        await ctx.reply(
            "❌ Couldn't sync slash commands for this server right now.",
            mention_author=False,
        )
        return

    log.info(
        "Synced %d global command(s) and cleared %d guild override command(s) in %s "
        "(requester=%s)",
        len(synced),
        len(cleared_guild),
        getattr(ctx.guild, "id", "unknown"),
        author.id,
    )
    await ctx.message.add_reaction("✅")


class MusicBot(commands.Bot):
    """Discord bot coordinating playback, controller views, and persona branding."""

    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.player = MusicPlayer(self)
        self.pending_deletes: dict[int, dict] = {}
        self.state = _BotVisualState()
        self.controller_message: discord.Message | None = None
        self.purge_code: str | None = None
        self.add_command(_sync_tree_command)

    async def setup_hook(self) -> None:
        """Register persistent views and sync the application command tree."""
        self.add_view(MusicControlView())
        self.add_view(ShadyControlView())
        await self.tree.sync()

    async def on_ready(self) -> None:
        """Initialize persistent services after the bot logs in."""
        log.info("Logged in as %s (id=%s)", self.user, self.user.id)  # type: ignore[union-attr]
        init_db()
        sync_ads_from_disk()
        sync_broadcasts_from_disk()
        self.player.set_rigged_songs(list(RIGGED_SONG_IDS))
        self._load_controller_state()
        self._sync_controller_mode_from_persona_mode()

        DJ_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        for day in DJ_DAYS:
            (DJ_EVENTS_DIR / day).mkdir(parents=True, exist_ok=True)
        self.player.set_dj_events(DJEventScheduler(DJ_EVENTS_DIR))

        try:
            await self._apply_branding_for_day(force=True)
        except (BrandingRateLimitError, discord.DiscordException, OSError, ValueError) as exc:
            log.warning("Initial branding apply failed: %s", exc)
        if self.state.branding_task is None or self.state.branding_task.done():
            self.state.branding_task = asyncio.create_task(self._branding_loop())

        self.player.on_track_start = self.update_controller_now_playing
        await self.announce_state(channel=self._controller_text_channel())
        await self._ensure_controller()

    async def run_upload_with_queue(
        self,
        worker: Callable[[], Awaitable[str]],
    ) -> tuple[int, str]:
        """Queue one upload operation and run it once earlier requests finish."""
        async with self.state.upload_queue.state_lock:
            self.state.upload_queue.pending_requests += 1
            queue_position = self.state.upload_queue.pending_requests
        try:
            async with self.state.upload_queue.lock:
                return queue_position, await worker()
        finally:
            async with self.state.upload_queue.state_lock:
                self.state.upload_queue.pending_requests = max(
                    0,
                    self.state.upload_queue.pending_requests - 1,
                )

    async def start_upload_with_queue(
        self,
        worker: Callable[[], Awaitable[str]],
        notifier: Callable[[int, str], Awaitable[None]],
    ) -> int:
        """Queue one upload operation and notify when it finishes."""
        async with self.state.upload_queue.state_lock:
            self.state.upload_queue.pending_requests += 1
            queue_position = self.state.upload_queue.pending_requests

        async def _runner() -> None:
            try:
                async with self.state.upload_queue.lock:
                    message = await worker()
            finally:
                async with self.state.upload_queue.state_lock:
                    self.state.upload_queue.pending_requests = max(
                        0,
                        self.state.upload_queue.pending_requests - 1,
                    )
            await notifier(queue_position, message)

        task = asyncio.create_task(_runner())
        task.add_done_callback(self._log_upload_task_failure)
        return queue_position

    @staticmethod
    def _log_upload_task_failure(task: asyncio.Task[None]) -> None:
        """Log unhandled upload task failures without breaking the queue."""
        try:
            task.result()
        except (
            discord.DiscordException,
            OSError,
            RuntimeError,
            ValueError,
        ):
            log.exception("Queued upload task failed.")

    @staticmethod
    def _upload_report_file(filename: str, message: str) -> discord.File:
        """Create a text attachment for a long upload report."""
        report_bytes = message.encode("utf-8", errors="replace")
        return discord.File(BytesIO(report_bytes), filename=filename)

    @staticmethod
    def _upload_notification_preview(title: str, message: str) -> str:
        """Build a bounded message preview for long upload reports."""
        lines = [line for line in message.splitlines() if line]
        preview_lines: list[str] = []
        total_length = len(title) + 1
        for line in lines:
            projected = total_length + len(line) + 1
            if preview_lines and projected > _UPLOAD_NOTIFICATION_PREVIEW_LIMIT:
                break
            if not preview_lines and projected > _UPLOAD_NOTIFICATION_PREVIEW_LIMIT:
                allowed = max(0, _UPLOAD_NOTIFICATION_PREVIEW_LIMIT - total_length - 2)
                preview_lines.append(f"{line[:allowed]}…")
                break
            preview_lines.append(line)
            total_length = projected
        if not preview_lines:
            preview_lines.append("The upload completed. Full report attached.")
        remaining = len(lines) - len(preview_lines)
        preview = "\n".join([title, *preview_lines])
        if remaining > 0:
            preview += f"\n📄 Full report attached ({remaining} more line(s))."
        else:
            preview += "\n📄 Full report attached."
        return preview

    async def notify_upload_completion(
        self,
        interaction: discord.Interaction,
        title: str,
        message: str,
        *,
        filename: str,
    ) -> None:
        """Deliver upload completion details outside the original interaction lifecycle."""
        content = f"{title}\n{message}"
        if len(content) <= 2000:
            try:
                await interaction.user.send(content)
                return
            except discord.Forbidden:
                pass
            except discord.DiscordException as exc:
                log.warning("Could not DM upload completion to %s: %s", interaction.user.id, exc)
        else:
            preview = self._upload_notification_preview(title, message)
            try:
                await interaction.user.send(
                    preview,
                    file=self._upload_report_file(filename, message),
                )
                return
            except discord.Forbidden:
                pass
            except discord.DiscordException as exc:
                log.warning("Could not DM upload completion to %s: %s", interaction.user.id, exc)

        channel = interaction.channel
        if channel is None:
            log.warning("Upload completion for %s had no channel fallback.", interaction.user.id)
            return
        try:
            if len(content) <= 2000:
                await channel.send(
                    f"{interaction.user.mention} {content}",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
                return
            await channel.send(
                f"{interaction.user.mention} {self._upload_notification_preview(title, message)}",
                file=self._upload_report_file(filename, message),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.DiscordException as exc:
            log.warning(
                "Could not send upload completion fallback in channel for %s: %s",
                interaction.user.id,
                exc,
            )

    def _controller_text_channel(self) -> discord.TextChannel | None:
        """Return the configured controller channel when available."""
        if not CONTROLLER_CHANNEL_ID:
            return None
        channel = self.get_channel(CONTROLLER_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return None
        return channel

    async def _ensure_controller(self) -> None:
        """Recreate the persistent controller message in the configured channel."""
        channel = self._controller_text_channel()
        if channel is None:
            return
        stale_controllers: list[discord.Message] = []
        async for message in channel.history(limit=CONTROLLER_SEARCH_LIMIT):
            if message.author == self.user and message.components:
                stale_controllers.append(message)
        for stale in stale_controllers:
            try:
                await stale.delete()
            except discord.DiscordException as exc:
                log.warning("Failed to delete old controller message %s: %s", stale.id, exc)
        self.controller_message = await channel.send(
            embed=self.active_controller_embed(),
            view=self.active_controller_view(),
        )
        log.info("Controller posted in #%s", channel.name)

    async def _branding_loop(self) -> None:
        """Periodically re-apply day-based branding changes."""
        while True:
            try:
                await self._apply_branding_for_day()
            except (BrandingRateLimitError, discord.DiscordException, OSError, ValueError) as exc:
                log.warning("Branding check failed: %s", exc)
            await asyncio.sleep(300)

    def _branding_target(self) -> str:
        """Return the current branding target based on persona mode and weekday."""
        if self.state.persona_mode == "heaven":
            return "heaven"
        if self.state.persona_mode == "collector":
            return "collector"
        weekday = datetime.datetime.now(datetime.timezone.utc).weekday()
        return "heaven" if weekday == 6 else "suffering"

    def _branding_assets(self, target: str) -> tuple[str, bytes | None, bytes | None]:
        """Return username, avatar bytes, and banner bytes for the target branding mode."""
        branding_map = {
            "heaven": (
                HEAVEN_BOT_NAME,
                _read_optional_bytes(HEAVEN_AVATAR_PATH),
                _read_optional_bytes(HEAVEN_BANNER_PATH),
            ),
            "collector": (
                COLLECTOR_BOT_NAME,
                _read_optional_bytes(COLLECTOR_AVATAR_PATH),
                _read_optional_bytes(COLLECTOR_BANNER_PATH),
            ),
            "suffering": (
                SUFFERING_BOT_NAME,
                _read_optional_bytes(SUFFERING_AVATAR_PATH),
                _read_optional_bytes(SUFFERING_BANNER_PATH),
            ),
        }
        return branding_map[target]

    async def _apply_branding_for_day(self, force: bool = False) -> None:
        """Apply the correct persona branding when the target mode changes."""
        if self.user is None:
            return
        target = self._branding_target()
        if not force and target == self.state.branding_mode:
            return

        username, avatar, banner = self._branding_assets(target)
        kwargs: dict[str, object] = {"username": username}
        if avatar is not None:
            kwargs["avatar"] = avatar
        if banner is not None:
            kwargs["banner"] = banner

        try:
            await self.user.edit(**kwargs)
            self.state.branding_mode = target
            await self._update_guild_nicknames(username)
            log.info("Branding switched to %s mode.", target)
        except discord.HTTPException as exc:
            if "too fast" in str(exc).lower():
                raise BrandingRateLimitError(str(exc)) from exc
            log.warning("Could not apply %s branding: %s", target, exc)
        except (discord.DiscordException, OSError, ValueError) as exc:
            log.warning("Could not apply %s branding: %s", target, exc)

    async def _update_guild_nicknames(self, nickname: str) -> None:
        """Update the bot nickname in every guild when possible."""
        if self.user is None:
            return
        for guild in self.guilds:
            member = guild.get_member(self.user.id)
            if member is None or member.nick == nickname:
                continue
            try:
                await member.edit(nick=nickname)
            except discord.DiscordException as exc:
                log.debug("Could not set nickname in guild %s: %s", guild.id, exc)

    def _current_brand_name(self) -> str:
        """Return the display name matching the current branding mode."""
        if self.state.branding_mode == "heaven":
            return HEAVEN_BOT_NAME
        if self.state.branding_mode == "collector":
            return COLLECTOR_BOT_NAME
        return SUFFERING_BOT_NAME

    def _sync_controller_mode_from_persona_mode(self) -> None:
        """Keep the controller mode aligned with the selected persona mode."""
        self.state.controller_mode = (
            "shady" if self.state.persona_mode == "collector" else "full"
        )

    def _load_controller_state(self) -> None:
        """Load the saved persona mode from disk when available."""
        try:
            if not CONTROLLER_STATE_PATH.exists():
                return
            raw = json.loads(CONTROLLER_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning("Failed to load controller state: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        mode = raw.get("persona_mode")
        if mode in {"collector", "suffering", "heaven"}:
            self.state.persona_mode = mode

    def _save_controller_state(self) -> None:
        """Persist the current persona mode to disk."""
        payload = {"persona_mode": self.state.persona_mode}
        try:
            CONTROLLER_STATE_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Failed to save controller state: %s", exc)

    def active_controller_view(self) -> discord.ui.View:
        """Return the controller view matching the current mode."""
        if self.state.controller_mode == "shady":
            return ShadyControlView()
        return MusicControlView()

    def _controller_embed_state(self, *, added_by: str | None = None) -> _ControllerEmbedState:
        """Build the state object used by the full controller embed."""
        return _ControllerEmbedState(
            title=self._current_brand_name(),
            song=self.state.controller_song,
            label=self.state.controller_label,
            added_by=added_by,
            is_paused=self.player.is_paused(),
            colour=self._state_colour(),
        )

    def active_controller_embed(self) -> discord.Embed:
        """Return the controller embed matching the current mode and playback state."""
        state = self._controller_embed_state()
        if self.state.controller_mode == "shady":
            return _shady_controller_embed(state)
        return _controller_embed(state)

    def _state_key(self) -> str:
        """Return the current high-level persona key."""
        if self.state.persona_mode == "heaven":
            return "heaven"
        if self.state.persona_mode == "collector":
            return "collector"
        weekday = datetime.datetime.now(datetime.timezone.utc).weekday()
        return "heaven" if weekday == 6 else "suffering"

    def _state_colour(self) -> discord.Colour:
        """Return the embed colour associated with the active persona state."""
        key = self._state_key()
        if key == "collector":
            return discord.Colour.purple()
        if key == "heaven":
            return discord.Colour.from_rgb(255, 215, 0)
        return discord.Colour.from_rgb(0, 0, 0)

    async def switch_persona_with_branding(self, persona_mode: str) -> bool:
        """Switch persona mode and roll back if branding changes are rate-limited."""
        previous_persona = self.state.persona_mode
        self.state.persona_mode = persona_mode
        self._sync_controller_mode_from_persona_mode()
        self._save_controller_state()
        try:
            await self._apply_branding_for_day()
        except BrandingRateLimitError:
            self.state.persona_mode = previous_persona
            self._sync_controller_mode_from_persona_mode()
            self._save_controller_state()
            try:
                await self._apply_branding_for_day()
            except (discord.DiscordException, OSError, ValueError) as exc:
                log.warning("Failed to restore prior branding after rate limit: %s", exc)
            await self.refresh_controller_status()
            return False
        await self.refresh_controller_status()
        return True

    async def _quoteable_moment(self) -> str:
        """Return a random quote from the quotes channel, or a fallback line."""
        channel = self.get_channel(QUOTES_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel):
            return "The archive stayed silent this time."
        quotes = [
            message.content
            async for message in channel.history(limit=10000)
            if len(message.content) >= 2
        ]
        if not quotes:
            return "The archive stayed silent this time."
        return f"You can't run from a past that haunts you...\n\n{random.choice(quotes)}"

    async def state_announcement(self) -> str:
        """Return the persona announcement line for the current mode."""
        key = self._state_key()
        if key == "collector":
            return "🕶️ The Collector has come to collect."
        if key == "heaven":
            return "✨ Heaven's gates have been thrown open."
        announcement = random.choice(_SUFFERING_LINES)
        if announcement != "quoteable_moment":
            return announcement
        return await self._quoteable_moment()

    async def announce_state(self, channel: discord.abc.Messageable | None) -> None:
        """Send the current persona announcement to the target channel."""
        target_channel = channel
        if target_channel is None:
            target_channel = self.controller_message.channel if self.controller_message else None
        if target_channel is None:
            return
        await send_chunked_channel_message(
            target_channel,
            await self.state_announcement(),
        )

    async def submit_current_song_feedback(
        self,
        interaction: discord.Interaction,
        is_like: bool,
    ) -> tuple[bool, str]:
        """Apply like or dislike feedback to the currently playing song."""
        song = self.player.current_song
        if not song:
            return False, "Nothing is currently playing."
        ok, message = apply_song_feedback(song["id"], interaction.user.id, is_like)
        if not ok:
            return False, message
        fresh = get_song(song["id"])
        if fresh:
            self.player.current_song = fresh
        return True, message

    async def refresh_controller_status(self) -> None:
        """Refresh the persistent controller message with current playback state."""
        if self.controller_message is None:
            return
        if self.state.controller_song:
            added_by = await _resolve_username(
                self,
                self.state.controller_song.get("added_by", ""),
                self.controller_message.guild,
            )
            embed_state = self._controller_embed_state(added_by=added_by)
        else:
            embed_state = self._controller_embed_state()
        if self.state.controller_mode == "shady":
            embed = _shady_controller_embed(embed_state)
        else:
            embed = _controller_embed(embed_state)

        try:
            await self.controller_message.edit(
                embed=embed,
                view=self.active_controller_view(),
            )
        except discord.DiscordException as exc:
            log.warning("Failed to update controller embed: %s", exc)

    async def clear_controller_now_playing(self) -> None:
        """Clear the now-playing data shown on the controller."""
        self.state.controller_song = None
        self.state.controller_label = None
        await self.refresh_controller_status()

    async def update_controller_now_playing(
        self,
        song: dict | None,
        label: str,
    ) -> None:
        """Update the persistent controller with the latest playback item."""
        if song:
            self.state.controller_song = song
            self.state.controller_label = None
        else:
            self.state.controller_song = None
            self.state.controller_label = label
        await self.refresh_controller_status()

    async def on_raw_reaction_add(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Handle legacy reaction-based delete confirmations."""
        if self.user and payload.user_id == self.user.id:
            return
        pending = self.pending_deletes.get(payload.message_id)
        if not pending or payload.user_id != pending["user_id"]:
            return

        channel = self.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        message = await channel.fetch_message(payload.message_id)
        song = pending["song"]
        emoji = str(payload.emoji)

        if emoji == REACT_DEACTIVATE:
            deactivate_song(song["id"])
            await message.edit(
                content=(
                    f"⛔ **{song['name']}** by **{song['artist']}** has been deactivated.\n"
                    "Use `/toggle_song` or `/toggle_song_id` to re-enable it."
                )
            )
            await message.clear_reactions()
            del self.pending_deletes[payload.message_id]
            return

        if emoji == REACT_HARD_DELETE:
            hard_delete_song(song["id"])
            song_path = SONGS_DIR / song["filename"]
            if song_path.exists():
                song_path.unlink()
            await message.edit(
                content=(
                    f"🗑️ **{song['name']}** by **{song['artist']}** "
                    "has been permanently deleted."
                )
            )
            await message.clear_reactions()
            del self.pending_deletes[payload.message_id]
            return

        if emoji == REACT_CANCEL:
            await message.edit(content="🚫 Delete action canceled.")
            await message.clear_reactions()
            del self.pending_deletes[payload.message_id]


def create_bot() -> MusicBot:
    """Create the configured bot instance."""
    return MusicBot()
