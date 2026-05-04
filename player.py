"""
player.py – MusicPlayer manages the voice connection, shuffle queue,
            and the rigged-song mechanic.

Rigged mechanic
---------------
Every time a new song is selected from the queue there is a
1-in-RIGGED_CHANCE probability that the designated "rigged" song is
played instead of whatever was next in the shuffled queue.  The rigged
song is inserted silently; callers only see the resulting song dict.
"""
import asyncio
import logging
import random
from typing import Optional

import discord

from config import FFMPEG_OPTIONS, RIGGED_CHANCE, SONGS_DIR
from database import get_all_songs, get_song, increment_play_count

log = logging.getLogger(__name__)


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

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_rigged_song(self, song_id: int) -> None:
        self._rigged_song_id = song_id

    @property
    def rigged_song_id(self) -> int:
        return self._rigged_song_id

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
        Pick and play the next song.

        Returns the song dict that started playing, or *None* if there
        is nothing to play (empty library or no voice connection).
        """
        if not self.voice_client or not self.voice_client.is_connected():
            return None

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
