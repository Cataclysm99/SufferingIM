"""
broadcast.py – DJ event clip selection utilities.

Folder layout:
    dj_events/<weekday>/intro.*
    dj_events/<weekday>/outro.*
    dj_events/<weekday>/<other event clips>
"""
from __future__ import annotations

import datetime
import random
from pathlib import Path
from typing import Optional

from config import ALLOWED_EXTENSIONS

DAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class DJEventScheduler:
    """Select intro, outro, and hourly DJ event clips by weekday."""

    def __init__(self, dj_dir: Path) -> None:
        self.dj_dir = dj_dir

    @staticmethod
    def today_name() -> str:
        """Return today's weekday name in lowercase."""
        return datetime.date.today().strftime("%A").lower()

    def _day_files(self, day: str) -> list[Path]:
        """Return supported media files for the given weekday folder."""
        day_dir = self.dj_dir / day
        if not day_dir.is_dir():
            return []
        return [
            file_path
            for file_path in day_dir.iterdir()
            if file_path.is_file() and file_path.suffix.lower() in ALLOWED_EXTENSIONS
        ]

    def intro_clip(self, day: str | None = None) -> Optional[Path]:
        """Return the intro clip for the requested day, if present."""
        day = day or self.today_name()
        for file_path in self._day_files(day):
            if file_path.stem.lower() == "intro":
                return file_path
        return None

    def outro_clip(self, day: str | None = None) -> Optional[Path]:
        """Return the outro clip for the requested day, if present."""
        day = day or self.today_name()
        for file_path in self._day_files(day):
            if file_path.stem.lower() == "outro":
                return file_path
        return None

    def random_hourly_clip(self, day: str | None = None) -> Optional[Path]:
        """Return a random non-intro and non-outro clip for the requested day."""
        day = day or self.today_name()
        pool = [
            file_path
            for file_path in self._day_files(day)
            if file_path.stem.lower() not in {"intro", "outro"}
        ]
        if not pool:
            return None
        return random.choice(pool)
