"""player.py – MusicPlayer with ads, DJ events, weighted songs, and rigged pool."""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import discord

try:
    import imageio_ffmpeg as _imageio_ffmpeg
except ImportError:
    _imageio_ffmpeg = None

from config import (
    AD_INTERVAL_MINUTES,
    ADS_DIR,
    DJ_CYCLE_HOURS,
    DJ_EVENT_INTERVAL_MINUTES,
    FFMPEG_BEFORE_OPTIONS,
    FFMPEG_EXECUTABLE,
    FFMPEG_OPTIONS,
    RIGGED_CHANCE,
    SONGS_DIR,
)
from database import (
    get_all_songs,
    get_random_ad,
    increment_broadcast_play_count,
    get_song,
    get_songs_by_ids,
    increment_ad_play_count,
    increment_play_count,
    reset_negative_vote_scores,
    reset_song_vote_score,
)

log = logging.getLogger(__name__)


if TYPE_CHECKING:
    from broadcast import DJEventScheduler


@dataclass(slots=True)
class _PlaybackCycleState:
    """Mutable state tracking ads, DJ events, and forced intermission clips."""

    forced_clip_path: Path | None = None
    forced_clip_payload: dict | None = None
    forced_label: str = "intermission"
    cycle_started_at: float = field(default_factory=time.monotonic)
    last_ad_at: float = field(default_factory=time.monotonic)
    last_dj_event_at: float = field(default_factory=time.monotonic)
    intro_pending: bool = True


def _resolve_ffmpeg_executable() -> str:
    """Resolve the ffmpeg executable path from config, system PATH, or imageio-ffmpeg."""
    configured = FFMPEG_EXECUTABLE.strip()
    if configured:
        invalid_configured_path = False
        try:
            if Path(configured).exists() or shutil.which(configured):
                return configured
        except (OSError, ValueError):
            log.warning("Could not validate configured FFMPEG_EXECUTABLE: %s", configured)
            invalid_configured_path = True
        if not invalid_configured_path:
            log.warning("Configured FFMPEG_EXECUTABLE not found: %s", configured)

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    if _imageio_ffmpeg is not None:
        try:
            bundled = _imageio_ffmpeg.get_ffmpeg_exe()
        except (OSError, RuntimeError, ValueError):
            bundled = ""
        if bundled:
            log.info("Using bundled ffmpeg executable from imageio-ffmpeg.")
            return bundled

    return "ffmpeg"


_FFMPEG_EXECUTABLE = _resolve_ffmpeg_executable()


def _audio_source(path: Path) -> discord.FFmpegPCMAudio:
    """Create an ffmpeg audio source for a local file or stream."""
    source = str(path)
    ffmpeg_kwargs = dict(FFMPEG_OPTIONS)
    is_stream_source = source.startswith(
        ("http://", "https://", "rtmp://", "rtsp://", "mms://")
    )
    if is_stream_source and FFMPEG_BEFORE_OPTIONS.strip():
        ffmpeg_kwargs["before_options"] = FFMPEG_BEFORE_OPTIONS
    return discord.FFmpegPCMAudio(source, executable=_FFMPEG_EXECUTABLE, **ffmpeg_kwargs)


class MusicPlayer:
    """Stateful music player attached to a single guild."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_song: Optional[dict] = None
        self._rigged_song_ids: list[int] = []
        self.dj_events: Optional["DJEventScheduler"] = None
        self._cycle = _PlaybackCycleState()
        self.on_track_start: Optional[Callable] = None

    async def _notify_track_start(self, song: Optional[dict], label: str) -> None:
        """Fire the on_track_start callback if one is registered."""
        if self.on_track_start is None:
            return
        try:
            await self.on_track_start(song, label)
        except (discord.DiscordException, RuntimeError) as exc:
            log.warning("on_track_start callback error: %s", exc)

    def set_rigged_songs(self, song_ids: list[int]) -> None:
        """Replace the rigged song pool with positive song IDs only."""
        self._rigged_song_ids = [song_id for song_id in song_ids if song_id > 0]

    @property
    def rigged_song_ids(self) -> tuple[int, ...]:
        """Return the configured rigged song IDs as an immutable tuple."""
        return tuple(self._rigged_song_ids)

    def set_dj_events(self, scheduler: "DJEventScheduler") -> None:
        """Attach the DJ event scheduler used for intros, outros, and hourly clips."""
        self.dj_events = scheduler

    def _connected_guild_voice(
        self,
        guild: discord.Guild,
    ) -> discord.VoiceClient | None:
        """Return the connected voice client for the given guild, if any."""
        voice = discord.utils.get(self.bot.voice_clients, guild=guild)
        if voice and voice.is_connected():
            return voice
        return None

    async def _move_voice_client(
        self,
        voice_client: discord.VoiceClient,
        channel: discord.VoiceChannel,
    ) -> None:
        """Move the provided voice client when it is in a different voice channel."""
        if voice_client.channel != channel:
            await voice_client.move_to(channel)

    async def _adopt_existing_voice(self, channel: discord.VoiceChannel) -> bool:
        """Reuse an existing voice client for the guild when possible."""
        guild_voice = self._connected_guild_voice(channel.guild)
        if guild_voice is not None:
            self.voice_client = guild_voice
            await self._move_voice_client(guild_voice, channel)
            return True
        if self.voice_client and self.voice_client.is_connected():
            await self._move_voice_client(self.voice_client, channel)
            return True
        return False

    async def _cleanup_stale_voice(self, guild: discord.Guild) -> None:
        """Disconnect a stale guild voice client after a connect timeout."""
        stale_voice = discord.utils.get(self.bot.voice_clients, guild=guild)
        if stale_voice and not stale_voice.is_connected():
            try:
                await stale_voice.disconnect(force=True)
            except discord.DiscordException:
                log.debug("Failed to disconnect stale voice client in guild %s.", guild.id)
        if self.voice_client and not self.voice_client.is_connected():
            self.voice_client = None

    # ------------------------------------------------------------------
    # Voice connection
    # ------------------------------------------------------------------

    async def connect(self, channel: discord.VoiceChannel) -> None:
        """Connect to voice or move the existing guild voice client."""
        try:
            if await self._adopt_existing_voice(channel):
                self._reset_cycle()
                return
            try:
                self.voice_client = await channel.connect()
            except discord.ClientException:
                if not await self._adopt_existing_voice(channel):
                    raise
        except TimeoutError:
            await self._cleanup_stale_voice(channel.guild)
            raise
        self._reset_cycle()

    async def disconnect(self) -> None:
        """Disconnect from voice and reset playback state."""
        if self.voice_client:
            await self.voice_client.disconnect()
            self.voice_client = None
        self.current_song = None
        self._reset_cycle()

    # ------------------------------------------------------------------
    # Intermission scheduling
    # ------------------------------------------------------------------

    def _reset_cycle(self) -> None:
        """Reset timers for the DJ cycle, ads, and intro clip."""
        now = time.monotonic()
        self._cycle.cycle_started_at = now
        self._cycle.last_ad_at = now
        self._cycle.last_dj_event_at = now
        self._cycle.intro_pending = True

    @staticmethod
    def _seconds(minutes: int) -> float:
        """Convert minutes to seconds as a float."""
        return float(minutes * 60)

    def _cycle_expired(self) -> bool:
        """Return True when the full DJ cycle duration has elapsed."""
        return (
            time.monotonic() - self._cycle.cycle_started_at
        ) >= float(DJ_CYCLE_HOURS * 3600)

    def _ad_due(self) -> bool:
        """Return True when it is time to play another ad."""
        return (time.monotonic() - self._cycle.last_ad_at) >= self._seconds(
            AD_INTERVAL_MINUTES
        )

    def _dj_due(self) -> bool:
        """Return True when it is time to play another hourly DJ clip."""
        return (time.monotonic() - self._cycle.last_dj_event_at) >= self._seconds(
            DJ_EVENT_INTERVAL_MINUTES
        )

    def _play_audio_file(self, path: Path, label: str) -> bool:
        """Play a local audio file and schedule the next playback step afterward."""
        if not (self.voice_client and self.voice_client.is_connected() and path.exists()):
            return False

        def _after(error: Optional[Exception]) -> None:
            if error:
                log.error("%s playback error: %s", label, error)
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        self.voice_client.play(_audio_source(path), after=_after)
        log.info("Playing %s: %s", label, path.name)
        return True

    async def _play_forced_clip(self) -> bool:
        """Play the currently queued forced clip, if one exists."""
        if self._cycle.forced_clip_path is None:
            return False
        path = self._cycle.forced_clip_path
        label = self._cycle.forced_label
        payload = self._cycle.forced_clip_payload
        self._cycle.forced_clip_path = None
        self._cycle.forced_clip_payload = None
        if self._play_audio_file(path, label):
            if label == "ad":
                self._cycle.last_ad_at = time.monotonic()
            elif label.startswith("dj"):
                self._cycle.last_dj_event_at = time.monotonic()
            if payload and payload.get("id"):
                increment_broadcast_play_count(payload["id"])
            await self._notify_track_start(payload, label)
            return True
        return False

    async def _play_dj_intro(self) -> bool:
        """Play the DJ intro clip once per cycle when available."""
        if self.dj_events is None:
            self._cycle.intro_pending = False
            return False
        clip = self.dj_events.intro_clip()
        self._cycle.intro_pending = False
        if clip and self._play_audio_file(clip["path"], "dj intro"):
            self._cycle.last_dj_event_at = time.monotonic()
            increment_broadcast_play_count(clip["id"])
            await self._notify_track_start(clip, "dj intro")
            return True
        return False

    async def _play_dj_outro_and_restart(self) -> bool:
        """Play the DJ outro clip and restart the cycle when it ends."""
        if self.dj_events is None:
            self._reset_cycle()
            reset_negative_vote_scores()
            return False
        clip = self.dj_events.outro_clip()
        self._reset_cycle()
        reset_negative_vote_scores()
        if clip and self._play_audio_file(clip["path"], "dj outro"):
            increment_broadcast_play_count(clip["id"])
            await self._notify_track_start(clip, "dj outro")
            return True
        return False

    async def _play_hourly_dj_event(self, day: str | None = None) -> bool:
        """Play a random hourly DJ event clip for the selected day."""
        if self.dj_events is None:
            return False
        clip = self.dj_events.random_hourly_clip(day)
        if clip and self._play_audio_file(clip["path"], "dj event"):
            self._cycle.last_dj_event_at = time.monotonic()
            increment_broadcast_play_count(clip["id"])
            await self._notify_track_start(clip, "dj event")
            return True
        return False

    async def _play_ad(self) -> bool:
        """Play a random ad when one is available."""
        ad = get_random_ad()
        if not ad:
            return False
        path = ADS_DIR / ad["filename"]
        if self._play_audio_file(path, "ad"):
            self._cycle.last_ad_at = time.monotonic()
            increment_ad_play_count(ad["id"])
            await self._notify_track_start(ad, "ad")
            return True
        return False

    async def _try_play_intermission(self) -> bool:
        """Play the next pending intermission clip, returning True when one starts."""
        if await self._play_forced_clip():
            return True
        if self._cycle.intro_pending and await self._play_dj_intro():
            return True
        if self._cycle_expired() and await self._play_dj_outro_and_restart():
            return True
        if self._ad_due() and await self._play_ad():
            return True
        return self._dj_due() and await self._play_hourly_dj_event()

    # ------------------------------------------------------------------
    # Public manual trigger APIs
    # ------------------------------------------------------------------

    async def play_dj_event_now(
        self,
        day: str | None = None,
        event_id: int | None = None,
    ) -> bool:
        """Queue a DJ event immediately, interrupting the current playback if needed."""
        if not (self.voice_client and self.voice_client.is_connected() and self.dj_events):
            return False
        clip = (
            self.dj_events.event_clip_by_id(event_id)
            if event_id is not None
            else self.dj_events.random_hourly_clip(day)
        )
        if clip is None:
            return False
        self._cycle.forced_clip_path = clip["path"]
        self._cycle.forced_clip_payload = clip
        self._cycle.forced_label = "dj event"
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()
            return True
        return await self._play_forced_clip()

    # ------------------------------------------------------------------
    # Song selection
    # ------------------------------------------------------------------

    def _pick_next_song(self) -> Optional[dict]:
        """Pick the next song using the rigged pool and vote-weighted selection rules."""
        songs = get_all_songs()
        if not songs:
            return None

        rigged_pool = get_songs_by_ids(self._rigged_song_ids)
        rigged_ids = {song["id"] for song in rigged_pool}
        normal_songs = [song for song in songs if song["id"] not in rigged_ids]
        if rigged_pool and random.randint(1, RIGGED_CHANCE) == 1:
            return random.choice(rigged_pool)

        selectable = [song for song in normal_songs if int(song.get("vote_score", 0)) >= 0]
        if not selectable:
            return random.choice(rigged_pool) if rigged_pool else None

        max_played = max(int(song.get("times_played", 0)) for song in selectable)
        max_weight = max_played + 1
        weights = []
        for song in selectable:
            vote_score = int(song.get("vote_score", 0))
            if vote_score > 0:
                weights.append(max_weight)
            else:
                weights.append(max_played - int(song.get("times_played", 0)) + 1)
        return random.choices(selectable, weights=weights, k=1)[0]

    def _next_playable_song(self) -> Optional[tuple[dict, Path]]:
        """Return the next playable song and its file path, skipping invalid entries."""
        while True:
            song = self._pick_next_song()
            if song is None:
                return None
            fresh = get_song(song["id"])
            if not fresh or not fresh.get("available", 1):
                continue
            song_path = SONGS_DIR / fresh["filename"]
            if not song_path.exists():
                continue
            return fresh, song_path

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    async def play_next(self) -> Optional[dict]:
        """Advance playback to the next intermission clip or song."""
        if not (self.voice_client and self.voice_client.is_connected()):
            return None
        if await self._try_play_intermission():
            return None

        next_song = self._next_playable_song()
        if next_song is None:
            self.current_song = None
            return None
        song, song_path = next_song
        self.current_song = song
        increment_play_count(song["id"])
        reset_song_vote_score(song["id"])

        def _after(error: Optional[Exception]) -> None:
            if error:
                log.error("Playback error: %s", error)
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        self.voice_client.play(_audio_source(song_path), after=_after)
        await self._notify_track_start(song, "song")
        return song

    def pause(self) -> bool:
        """Pause the current voice client if it is actively playing."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            return True
        return False

    def resume(self) -> bool:
        """Resume the current voice client if it is paused."""
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            return True
        return False

    def skip(self) -> bool:
        """Stop the current source so playback advances to the next item."""
        if self.voice_client and (
            self.voice_client.is_playing() or self.voice_client.is_paused()
        ):
            self.voice_client.stop()
            return True
        return False

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Return True when the bot is connected to voice."""
        return bool(self.voice_client and self.voice_client.is_connected())

    def is_playing(self) -> bool:
        """Return True when the bot is actively playing audio."""
        return bool(self.voice_client and self.voice_client.is_playing())

    def is_paused(self) -> bool:
        """Return True when the current voice client is paused."""
        return bool(self.voice_client and self.voice_client.is_paused())

    def is_active(self) -> bool:
        """Return True when the player is either playing or paused."""
        return self.is_playing() or self.is_paused()
