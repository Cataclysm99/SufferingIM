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

# Lowercase weekday names, Monday-first.
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
    """Selects intro/outro/hourly DJ event clips by weekday."""

    def __init__(self, dj_dir: Path) -> None:
        self.dj_dir = dj_dir

    @staticmethod
    def today_name() -> str:
        return datetime.date.today().strftime("%A").lower()

    def _day_files(self, day: str) -> list[Path]:
        day_dir = self.dj_dir / day
        if not day_dir.is_dir():
            return []
        return [
            f
            for f in day_dir.iterdir()
            if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS
        ]

    def intro_clip(self, day: str | None = None) -> Optional[Path]:
        day = day or self.today_name()
        for f in self._day_files(day):
            if f.stem.lower() == "intro":
                return f
        return None

    def outro_clip(self, day: str | None = None) -> Optional[Path]:
        day = day or self.today_name()
        for f in self._day_files(day):
            if f.stem.lower() == "outro":
                return f
        return None

    def random_hourly_clip(self, day: str | None = None) -> Optional[Path]:
        day = day or self.today_name()
        pool = [
            f
            for f in self._day_files(day)
            if f.stem.lower() not in {"intro", "outro"}
        ]
        if not pool:
            return None
        return random.choice(pool)

