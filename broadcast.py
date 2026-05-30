"""broadcast.py – DJ event clip selection utilities."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from database import get_broadcast_by_id, get_broadcast_clip

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

    def _clip(self, day: str, slot: str) -> Optional[dict]:
        """Return one broadcast row for the given day and slot."""
        clip = get_broadcast_clip(day, slot)
        if clip is None:
            return None
        clip["path"] = self.dj_dir / day / clip["filename"]
        if not clip["path"].exists():
            return None
        return clip

    def intro_clip(self, day: str | None = None) -> Optional[dict]:
        """Return intro broadcast metadata for the requested day, if present."""
        selected_day = day or self.today_name()
        return self._clip(selected_day, "intro")

    def outro_clip(self, day: str | None = None) -> Optional[dict]:
        """Return outro broadcast metadata for the requested day, if present."""
        selected_day = day or self.today_name()
        return self._clip(selected_day, "outro")

    def random_hourly_clip(self, day: str | None = None) -> Optional[dict]:
        """Return random event broadcast metadata for the requested day."""
        selected_day = day or self.today_name()
        clip = self._clip(selected_day, "event")
        if clip is None:
            return None
        return clip

    def event_clip_by_id(self, broadcast_id: int) -> Optional[dict]:
        """Return event broadcast metadata for an explicit broadcast ID."""
        clip = get_broadcast_by_id(broadcast_id)
        if clip is None or clip.get("slot") != "event":
            return None
        clip["path"] = self.dj_dir / clip["day"] / clip["filename"]
        if not clip["path"].exists():
            return None
        return clip
