"""
player.py – MusicPlayer with ads, DJ events, weighted songs, and rigged pool.
"""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import discord

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
    get_song,
    get_songs_by_ids,
    increment_ad_play_count,
    increment_play_count,
    reset_negative_vote_scores,
    reset_song_vote_score,
)

log = logging.getLogger(__name__)


def _resolve_ffmpeg_executable() -> str:
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

    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled:
            log.info("Using bundled ffmpeg executable from imageio-ffmpeg.")
            return bundled
    except Exception:
        pass

    return "ffmpeg"


_FFMPEG_EXECUTABLE = _resolve_ffmpeg_executable()


def _audio_source(path: Path) -> discord.FFmpegPCMAudio:
    ffmpeg_kwargs = dict(FFMPEG_OPTIONS)
    if FFMPEG_BEFORE_OPTIONS.strip():
        ffmpeg_kwargs["before_options"] = FFMPEG_BEFORE_OPTIONS
    return discord.FFmpegPCMAudio(
        str(path),
        executable=_FFMPEG_EXECUTABLE,
        **ffmpeg_kwargs,
    )

if TYPE_CHECKING:
    from broadcast import DJEventScheduler


class MusicPlayer:
    """Stateful music player attached to a single guild."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_song: Optional[dict] = None
        self._rigged_song_ids: list[int] = []
        self.dj_events: Optional["DJEventScheduler"] = None

        self._forced_clip_path: Optional[Path] = None
        self._forced_label: str = "intermission"

        self._cycle_started_at: float = time.monotonic()
        self._last_ad_at: float = self._cycle_started_at
        self._last_dj_event_at: float = self._cycle_started_at
        self._intro_pending: bool = True
        self.on_track_start: Optional[Callable] = None

    async def _notify_track_start(self, song: Optional[dict], label: str) -> None:
        """Fire on_track_start callback if registered, silently ignoring errors."""
        if self.on_track_start is not None:
            try:
                await self.on_track_start(song, label)
            except Exception as exc:
                log.warning("on_track_start callback error: %s", exc)

    def set_rigged_songs(self, song_ids: list[int]) -> None:
        self._rigged_song_ids = [sid for sid in song_ids if sid > 0]

    @property
    def rigged_song_ids(self) -> tuple[int, ...]:
        return tuple(self._rigged_song_ids)

    def set_dj_events(self, scheduler: "DJEventScheduler") -> None:
        self.dj_events = scheduler

    # ------------------------------------------------------------------
    # Voice connection
    # ------------------------------------------------------------------

    async def connect(self, channel: discord.VoiceChannel) -> None:
        guild_voice = discord.utils.get(self.bot.voice_clients, guild=channel.guild)
        if guild_voice and guild_voice.is_connected():
            self.voice_client = guild_voice
            if guild_voice.channel != channel:
                await guild_voice.move_to(channel)
        elif self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel != channel:
                await self.voice_client.move_to(channel)
        else:
            try:
                self.voice_client = await channel.connect()
            except discord.ClientException:
                guild_voice = discord.utils.get(self.bot.voice_clients, guild=channel.guild)
                if guild_voice and guild_voice.is_connected():
                    self.voice_client = guild_voice
                    if guild_voice.channel != channel:
                        await guild_voice.move_to(channel)
                else:
                    raise
        self._reset_cycle()

    async def disconnect(self) -> None:
        if self.voice_client:
            await self.voice_client.disconnect()
            self.voice_client = None
        self.current_song = None
        self._reset_cycle()

    # ------------------------------------------------------------------
    # Intermission scheduling
    # ------------------------------------------------------------------

    def _reset_cycle(self) -> None:
        now = time.monotonic()
        self._cycle_started_at = now
        self._last_ad_at = now
        self._last_dj_event_at = now
        self._intro_pending = True

    @staticmethod
    def _seconds(minutes: int) -> float:
        return float(minutes * 60)

    def _cycle_expired(self) -> bool:
        return (time.monotonic() - self._cycle_started_at) >= float(DJ_CYCLE_HOURS * 3600)

    def _ad_due(self) -> bool:
        return (time.monotonic() - self._last_ad_at) >= self._seconds(AD_INTERVAL_MINUTES)

    def _dj_due(self) -> bool:
        return (time.monotonic() - self._last_dj_event_at) >= self._seconds(DJ_EVENT_INTERVAL_MINUTES)

    def _play_audio_file(self, path: Path, label: str) -> bool:
        if not (self.voice_client and self.voice_client.is_connected()):
            return False
        if not path.exists():
            return False

        def _after(error: Optional[Exception]) -> None:
            if error:
                log.error("%s playback error: %s", label, error)
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        self.voice_client.play(
            _audio_source(path),
            after=_after,
        )
        log.info("Playing %s: %s", label, path.name)
        return True

    async def _play_forced_clip(self) -> bool:
        if self._forced_clip_path is None:
            return False
        path = self._forced_clip_path
        label = self._forced_label
        self._forced_clip_path = None
        if self._play_audio_file(path, label):
            if label == "ad":
                self._last_ad_at = time.monotonic()
            elif label.startswith("dj"):
                self._last_dj_event_at = time.monotonic()
            await self._notify_track_start(None, label)
            return True
        return False

    async def _play_dj_intro(self) -> bool:
        if self.dj_events is None:
            self._intro_pending = False
            return False
        clip = self.dj_events.intro_clip()
        self._intro_pending = False
        if clip and self._play_audio_file(clip, "dj intro"):
            self._last_dj_event_at = time.monotonic()
            await self._notify_track_start(None, "dj intro")
            return True
        return False

    async def _play_dj_outro_and_restart(self) -> bool:
        if self.dj_events is None:
            self._reset_cycle()
            reset_negative_vote_scores()
            return False
        clip = self.dj_events.outro_clip()
        self._reset_cycle()
        reset_negative_vote_scores()
        if clip and self._play_audio_file(clip, "dj outro"):
            await self._notify_track_start(None, "dj outro")
            return True
        return False

    async def _play_hourly_dj_event(self, day: str | None = None) -> bool:
        if self.dj_events is None:
            return False
        clip = self.dj_events.random_hourly_clip(day)
        if clip and self._play_audio_file(clip, "dj event"):
            self._last_dj_event_at = time.monotonic()
            await self._notify_track_start(None, "dj event")
            return True
        return False

    async def _play_ad(self) -> bool:
        ad = get_random_ad()
        if not ad:
            return False
        path = ADS_DIR / ad["filename"]
        if self._play_audio_file(path, "ad"):
            self._last_ad_at = time.monotonic()
            increment_ad_play_count(ad["id"])
            await self._notify_track_start(None, "ad")
            return True
        return False

    # ------------------------------------------------------------------
    # Public manual trigger APIs
    # ------------------------------------------------------------------

    async def play_dj_event_now(self, day: str | None = None) -> bool:
        if not (self.voice_client and self.voice_client.is_connected()):
            return False
        if self.dj_events is None:
            return False
        clip = self.dj_events.random_hourly_clip(day)
        if clip is None:
            return False
        self._forced_clip_path = clip
        self._forced_label = "dj event"
        if self.voice_client.is_playing() or self.voice_client.is_paused():
            self.voice_client.stop()
            return True
        return await self._play_forced_clip()

    # ------------------------------------------------------------------
    # Song selection
    # ------------------------------------------------------------------

    def _pick_next_song(self) -> Optional[dict]:
        songs = get_all_songs()
        if not songs:
            return None

        rigged_pool = get_songs_by_ids(self._rigged_song_ids)
        rigged_ids = {s["id"] for s in rigged_pool}
        normal_songs = [s for s in songs if s["id"] not in rigged_ids]

        if rigged_pool and random.randint(1, RIGGED_CHANCE) == 1:
            return random.choice(rigged_pool)

        # Exclude songs whose vote_score is negative.
        # In SufferingFM semantics, popular (liked) songs are suppressed; disliked songs are boosted.
        selectable = [s for s in normal_songs if int(s.get("vote_score", 0)) >= 0]

        if not selectable:
            if rigged_pool:
                return random.choice(rigged_pool)
            return None

        # Weight calculation:
        #   vote_score > 0 (received more dislikes than likes) → max possible weight (highest chance)
        #   vote_score == 0                                    → inverse of times_played
        max_played = max(int(s.get("times_played", 0)) for s in selectable)
        max_weight = max_played + 1  # weight a never-played song would receive

        weights = []
        for s in selectable:
            vs = int(s.get("vote_score", 0))
            if vs > 0:
                weights.append(max_weight)
            else:
                weights.append(max_played - int(s.get("times_played", 0)) + 1)

        return random.choices(selectable, weights=weights, k=1)[0]

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    async def play_next(self) -> Optional[dict]:
        if not (self.voice_client and self.voice_client.is_connected()):
            return None

        if await self._play_forced_clip():
            return None

        if self._intro_pending and await self._play_dj_intro():
            return None

        if self._cycle_expired() and await self._play_dj_outro_and_restart():
            return None

        if self._ad_due() and await self._play_ad():
            return None

        if self._dj_due() and await self._play_hourly_dj_event():
            return None

        song = self._pick_next_song()
        if song is None:
            self.current_song = None
            return None

        fresh = get_song(song["id"])
        if not fresh or not fresh.get("available", 1):
            return await self.play_next()

        song_path = SONGS_DIR / song["filename"]
        if not song_path.exists():
            return await self.play_next()

        self.current_song = fresh
        increment_play_count(song["id"])
        reset_song_vote_score(song["id"])

        def _after(error: Optional[Exception]) -> None:
            if error:
                log.error("Playback error: %s", error)
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        self.voice_client.play(
            _audio_source(song_path),
            after=_after,
        )
        await self._notify_track_start(fresh, "song")
        return fresh

    def pause(self) -> bool:
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            return True
        return False

    def resume(self) -> bool:
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            return True
        return False

    def skip(self) -> bool:
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
        return bool(self.voice_client and self.voice_client.is_connected())

    def is_playing(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_playing())

    def is_paused(self) -> bool:
        return bool(self.voice_client and self.voice_client.is_paused())

    def is_active(self) -> bool:
        return self.is_playing() or self.is_paused()
