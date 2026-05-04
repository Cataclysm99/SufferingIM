"""
player.py – MusicPlayer manages the voice connection, shuffle queue,
            the rigged-song mechanic, and the radio broadcast scheduler.

Rigged mechanic
---------------
Every time a new song is selected from the queue there is a
1-in-RIGGED_CHANCE probability that the designated "rigged" song is
played instead of whatever was next in the shuffled queue.  The rigged
song is inserted silently; callers only see the resulting song dict.

Radio broadcast
---------------
After every BROADCAST_INTERVAL regular songs, if the voice channel has
at least BROADCAST_MIN_USERS non-bot members, the next clip from
BroadcastScheduler is silently inserted before the next song.  The
counter resets after each broadcast.  Clips can also be force-played
immediately by a Music Manager via /play_broadcast.
"""
import asyncio
import logging
import random
from pathlib import Path
from typing import Optional

import discord

from config import BROADCAST_INTERVAL, BROADCAST_MIN_USERS, FFMPEG_OPTIONS, RIGGED_CHANCE, SONGS_DIR
from database import get_all_songs, get_song, increment_play_count

log = logging.getLogger(__name__)

# Avoid a circular import: BroadcastScheduler is referenced only as a type.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from broadcast import BroadcastScheduler


class MusicPlayer:
    """Stateful music player attached to a single guild."""

    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.voice_client: Optional[discord.VoiceClient] = None
        self.current_song: Optional[dict] = None

        # Shuffled play-queue; rebuilt automatically when empty.
        self._queue: list[dict] = []

        # Id of the song to secretly inject (0 = disabled).
        self._rigged_song_id: int = 0

        # Broadcast scheduler; None until set by the bot on startup.
        self.broadcast: Optional["BroadcastScheduler"] = None

        # Counts regular songs played since the last broadcast clip.
        self._songs_since_broadcast: int = 0

        # When True, the next play_next() call plays a broadcast clip immediately,
        # bypassing the interval counter and user-count check.
        self._force_broadcast: bool = False

        # When set, _play_broadcast_clip will use this path directly instead of
        # calling consume_next_clip().  Cleared immediately after use.
        self._forced_clip_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_rigged_song(self, song_id: int) -> None:
        self._rigged_song_id = song_id

    @property
    def rigged_song_id(self) -> int:
        return self._rigged_song_id

    def set_broadcast(self, scheduler: "BroadcastScheduler") -> None:
        """Attach the BroadcastScheduler that provides daily intermission clips."""
        self.broadcast = scheduler

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------

    def _should_play_broadcast(self) -> bool:
        """Return True when all conditions for an automatic broadcast insertion are met.

        Conditions (all must hold):
        * A BroadcastScheduler is attached.
        * Enough regular songs have played since the last broadcast.
        * The voice channel has at least BROADCAST_MIN_USERS non-bot members.
        * There is a clip available for today.
        """
        if self.broadcast is None:
            return False
        if self._songs_since_broadcast < BROADCAST_INTERVAL:
            return False
        if not (self.voice_client and self.voice_client.channel):
            return False
        user_count = sum(
            1 for m in self.voice_client.channel.members if not m.bot
        )
        if user_count < BROADCAST_MIN_USERS:
            return False
        return self.broadcast.peek_next_clip() is not None

    async def _play_broadcast_clip(self) -> bool:
        """Consume the next broadcast clip for today and start playing it.

        If ``_forced_clip_path`` is set it is used directly (and cleared)
        instead of consuming the next sequential clip.
        Resets the song-since-broadcast counter.
        Returns True if a clip was started, False if no clip is available
        or the voice client is not connected.
        """
        if not (self.voice_client and self.voice_client.is_connected()):
            return False
        if self.broadcast is None:
            return False

        if self._forced_clip_path is not None:
            clip_path: Optional[Path] = self._forced_clip_path
            self._forced_clip_path = None
        else:
            clip_path = self.broadcast.consume_next_clip()

        if clip_path is None:
            return False

        self._songs_since_broadcast = 0

        def _after(error: Optional[Exception]) -> None:
            if error:
                log.error("Broadcast playback error: %s", error)
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        self.voice_client.play(
            discord.FFmpegPCMAudio(str(clip_path), **FFMPEG_OPTIONS),
            after=_after,
        )
        log.info("Playing broadcast clip: %s", clip_path.name)
        return True

    async def play_broadcast_now(
        self, clip_path: Optional[Path] = None
    ) -> bool:
        """Force a broadcast clip to play immediately (Music Manager override).

        If *clip_path* is provided that exact file is played; otherwise the
        next sequential clip for today is used.

        If a song is currently playing it is stopped first; the ``_after``
        callback then picks up the broadcast via the ``_force_broadcast`` flag
        so there is no double-play race.

        Returns True if a clip will be (or has been) started.
        """
        if not (self.voice_client and self.voice_client.is_connected()):
            return False
        if self.broadcast is None:
            return False

        if clip_path is None:
            # Sequential mode: make sure there is a next clip.
            if self.broadcast.peek_next_clip() is None:
                return False
        else:
            # Explicit clip: store it so _play_broadcast_clip picks it up.
            self._forced_clip_path = clip_path

        if self.voice_client.is_playing() or self.voice_client.is_paused():
            # Signal play_next (triggered by the _after of the stopped song)
            # to play a broadcast clip rather than the next queue item.
            self._force_broadcast = True
            self.voice_client.stop()
            return True

        # Nothing playing — directly start the clip.
        return await self._play_broadcast_clip()

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def _build_queue(self) -> None:
        """Populate and shuffle the queue from the full song library."""
        songs = get_all_songs()
        if not songs:
            self._queue = []
            return
        self._queue = list(songs)
        random.shuffle(self._queue)

    def _pick_next(self) -> Optional[dict]:
        """
        Return the next song to play, applying the rigged-song mechanic.

        With probability 1/RIGGED_CHANCE the rigged song is returned
        regardless of queue position (the queue item that would have
        played is left in-place for the next pick).
        """
        # Rigged roll
        if self._rigged_song_id and random.randint(1, RIGGED_CHANCE) == 1:
            rigged = get_song(self._rigged_song_id)
            if rigged and rigged.get("available", 1):
                return rigged

        # Normal queue pick
        if not self._queue:
            self._build_queue()
        if self._queue:
            return self._queue.pop(0)
        return None

    # ------------------------------------------------------------------
    # Voice connection
    # ------------------------------------------------------------------

    async def connect(self, channel: discord.VoiceChannel) -> None:
        """Join or move to *channel*."""
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.move_to(channel)
        else:
            self.voice_client = await channel.connect()

    async def disconnect(self) -> None:
        """Leave the voice channel and reset state."""
        if self.voice_client:
            await self.voice_client.disconnect()
            self.voice_client = None
        self.current_song = None

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    async def play_next(self) -> Optional[dict]:
        """
        Pick and play the next song (or broadcast clip).

        Priority order:
        1. Force-broadcast flag set (Music Manager manual override).
        2. Automatic broadcast: interval elapsed and user-count threshold met.
        3. Normal shuffled queue (with rigged-song mechanic).

        Returns the song dict that started playing, or *None* if there
        is nothing to play (empty library or no voice connection) or when
        a broadcast clip was started instead of a regular song.
        """
        if not self.voice_client or not self.voice_client.is_connected():
            return None

        # 1. Force-broadcast (manual override via play_broadcast_now).
        if self._force_broadcast:
            self._force_broadcast = False
            if await self._play_broadcast_clip():
                return None

        # 2. Automatic broadcast insertion.
        if self._should_play_broadcast():
            if await self._play_broadcast_clip():
                return None

        # 3. Normal song.
        song = self._pick_next()
        if song is None:
            self.current_song = None
            return None

        # Re-fetch to confirm the song is still available (it may have been
        # deactivated after the queue was built but before playback started).
        fresh = get_song(song["id"])
        if not fresh or not fresh.get("available", 1):
            return await self.play_next()

        song_path = SONGS_DIR / song["filename"]
        if not song_path.exists():
            # Skip missing files silently and try the next one.
            return await self.play_next()

        self.current_song = song
        self._songs_since_broadcast += 1
        increment_play_count(song["id"])

        def _after(error: Optional[Exception]) -> None:
            if error:
                log.error("Playback error: %s", error)
            # Schedule the next song on the event loop.
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        self.voice_client.play(
            discord.FFmpegPCMAudio(str(song_path), **FFMPEG_OPTIONS),
            after=_after,
        )
        return song

    def pause(self) -> bool:
        """Pause playback. Returns *True* on success."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            return True
        return False

    def resume(self) -> bool:
        """Resume playback. Returns *True* on success."""
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            return True
        return False

    def skip(self) -> bool:
        """
        Stop the current song (triggering the *after* callback which
        will start the next one).  Returns *True* on success.
        """
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
