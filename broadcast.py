"""
broadcast.py – Daily radio broadcast scheduler.

File layout
-----------
Broadcast clips are placed under::

    radio/
        monday/    01.mp3  02.mp3  ...
        tuesday/   01.mp3  02.mp3  ...
        wednesday/ 01.mp3  02.mp3  ...
        thursday/  01.mp3  02.mp3  ...
        friday/    01.mp3  02.mp3  ...
        saturday/  01.mp3  02.mp3  ...
        sunday/    01.mp3  02.mp3  ...

Clips are sorted **numerically** by filename stem so zero-padded names
(``01``, ``02``, …, ``10``) and plain names (``1``, ``2``, …, ``10``)
both order correctly.

Playback rules
--------------
* Clips for the current weekday play forward sequentially — no repeats
  within a week.
* At the start of each new ISO week the index resets to the first clip,
  so the story begins again regardless of how many clips were heard.
* State is persisted to ``radio_state.json`` in the project root so it
  survives bot restarts.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from typing import Optional

from config import ALLOWED_EXTENSIONS, BASE_DIR

log = logging.getLogger(__name__)

# Lowercase weekday names, Monday-first (matches datetime.weekday() order).
DAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_STATE_FILE: Path = BASE_DIR / "radio_state.json"


# ---------------------------------------------------------------------------
# Natural-sort helper
# ---------------------------------------------------------------------------

def _natural_sort_key(p: Path) -> tuple:
    """Return a sort key that orders numeric filename stems correctly.

    ``01``, ``1``, ``002`` all compare as integers rather than strings,
    so ``10.mp3`` sorts after ``9.mp3`` rather than between ``1.mp3``
    and ``2.mp3``.
    """
    parts = re.split(r"(\d+)", p.stem)
    return tuple(int(x) if x.isdigit() else x.lower() for x in parts)


# ---------------------------------------------------------------------------
# BroadcastScheduler
# ---------------------------------------------------------------------------

class BroadcastScheduler:
    """Manages the sequential playback index for each day's radio clips.

    State structure (``radio_state.json``)::

        {
            "monday":    {"index": 3, "week": 18},
            "tuesday":   {"index": 0, "week": 18},
            ...
        }

    ``index`` is the 0-based position of the *next* clip to play.
    ``week`` is the ISO week number when the index was last written.
    When the current ISO week differs from ``week`` the index resets to 0.
    """

    def __init__(self, radio_dir: Path) -> None:
        self.radio_dir = radio_dir
        self._state: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if _STATE_FILE.exists():
            try:
                self._state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                return
            except Exception as exc:
                log.warning(
                    "Could not read %s: %s — starting with a fresh index.",
                    _STATE_FILE.name,
                    exc,
                )
        self._state = {}

    def _save(self) -> None:
        try:
            _STATE_FILE.write_text(
                json.dumps(self._state, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            log.error("Could not save %s: %s", _STATE_FILE.name, exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _current_week() -> int:
        """Return the current ISO week number (1–53)."""
        return datetime.date.today().isocalendar()[1]

    @staticmethod
    def today_name() -> str:
        """Return today's lowercase weekday name (e.g. ``'monday'``)."""
        return datetime.date.today().strftime("%A").lower()

    def _clips(self, day: str) -> list[Path]:
        """Return numerically sorted audio clips for *day* (empty list if absent)."""
        day_dir = self.radio_dir / day
        if not day_dir.is_dir():
            return []
        files = [
            f
            for f in day_dir.iterdir()
            if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS
        ]
        return sorted(files, key=_natural_sort_key)

    def _state_for(self, day: str) -> dict:
        """Return (and reset-if-stale) the mutable playback state for *day*."""
        week = self._current_week()
        s = self._state.get(day)
        if s is None or s.get("week") != week:
            s = {"index": 0, "week": week}
            self._state[day] = s
        return s

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def peek_next_clip(self, day: str | None = None) -> Optional[Path]:
        """Return the next clip path for *day* **without** advancing the index.

        Returns ``None`` when no clips exist for the day or all have been played.
        """
        day = day or self.today_name()
        clips = self._clips(day)
        idx = self._state_for(day)["index"]
        if idx >= len(clips):
            return None
        return clips[idx]

    def consume_next_clip(self, day: str | None = None) -> Optional[Path]:
        """Return the next clip for *day* and advance the index.

        Skips clips whose file is missing.  Persists the updated index.
        Returns ``None`` when all clips have been played.
        """
        day = day or self.today_name()
        clips = self._clips(day)
        s = self._state_for(day)

        while s["index"] < len(clips):
            clip = clips[s["index"]]
            s["index"] += 1
            self._save()
            if clip.exists():
                log.info(
                    "Broadcast: serving clip %s (now at index %d/%d for %s)",
                    clip.name,
                    s["index"],
                    len(clips),
                    day,
                )
                return clip
            log.warning("Broadcast: clip %s missing on disk — skipping.", clip)

        return None

    def clips_remaining(self, day: str | None = None) -> int:
        """Return the number of clips still to play for *day* this week."""
        day = day or self.today_name()
        clips = self._clips(day)
        idx = self._state_for(day)["index"]
        return max(0, len(clips) - idx)

    def total_clips(self, day: str | None = None) -> int:
        """Return the total number of audio clips available for *day*."""
        day = day or self.today_name()
        return len(self._clips(day))

    def next_clip_number(self, day: str | None = None) -> int:
        """Return the 1-based number of the clip that will play next (0 if none)."""
        day = day or self.today_name()
        idx = self._state_for(day)["index"]
        clips = self._clips(day)
        return (idx + 1) if idx < len(clips) else 0

    def get_clip_path(self, day: str, clip_number: int) -> Optional[Path]:
        """Return the path for the 1-based *clip_number* for *day* without
        changing the sequential index.  Returns ``None`` if out of range.
        """
        clips = self._clips(day)
        idx = clip_number - 1
        if idx < 0 or idx >= len(clips):
            return None
        return clips[idx]

    def consume_clip_at(self, day: str, clip_number: int) -> Optional[Path]:
        """Play a specific 1-based clip and advance the sequential index past it.

        Sets the next-to-play index to *clip_number* so that the story
        continues forward from the clip after the one explicitly chosen.
        Returns ``None`` if *clip_number* is out of range or the file is missing.
        """
        clips = self._clips(day)
        idx = clip_number - 1
        if idx < 0 or idx >= len(clips):
            return None
        clip = clips[idx]
        # Advance the sequential index to just after this clip.
        s = self._state_for(day)
        s["index"] = idx + 1
        self._save()
        if not clip.exists():
            log.warning(
                "Broadcast: specific clip %s missing on disk — cannot play.", clip
            )
            return None
        log.info(
            "Broadcast: serving specific clip %s (sequential index set to %d for %s)",
            clip.name,
            s["index"],
            day,
        )
        return clip
