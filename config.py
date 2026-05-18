"""
config.py – centralised configuration loaded from environment / .env file.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Discord ──────────────────────────────────────────────────────────────────
TOKEN: str = os.getenv("DISCORD_TOKEN", "")


def _int_env(key: str, default: int = 0) -> int:
    """Parse an integer environment variable, returning *default* on failure."""
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _int_list_env(key: str) -> tuple[int, ...]:
    """Parse a comma-separated list of ints from an env var."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return ()
    values: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.append(int(part))
        except ValueError:
            continue
    return tuple(values)


# Channel where the persistent controller message is (re-)posted on startup.
# Leave empty / 0 to skip auto-posting.
CONTROLLER_CHANNEL_ID: int = _int_env("CONTROLLER_CHANNEL_ID", 0)

# ── Permissions ───────────────────────────────────────────────────────────────
# Discord role ID whose members may add, delete, and toggle songs.
# Set to 0 (or omit) to allow everyone to manage the library (unrestricted mode).
MUSIC_MANAGER_ROLE_ID: int = _int_env("MUSIC_MANAGER_ROLE_ID", 0)

# ── Rigged song ───────────────────────────────────────────────────────────────
# Database row-ids of songs that are secretly inserted into playback.
# Comma-separated ids, e.g. "3,7,12". Leave empty to disable.
RIGGED_SONG_IDS: tuple[int, ...] = _int_list_env("RIGGED_SONG_IDS")

# Odds: 1-in-RIGGED_CHANCE probability of playing the rigged song next.
RIGGED_CHANCE: int = max(1, _int_env("RIGGED_CHANCE", 10))

# ── Intermissions ──────────────────────────────────────────────────────────────
# Approximate ad cadence while playback is running.
AD_INTERVAL_MINUTES: int = max(1, _int_env("AD_INTERVAL_MINUTES", 30))

# Approximate DJ event cadence while playback is running.
DJ_EVENT_INTERVAL_MINUTES: int = max(1, _int_env("DJ_EVENT_INTERVAL_MINUTES", 60))

# Length of one DJ cycle before outro then restart from intro.
DJ_CYCLE_HOURS: int = max(1, _int_env("DJ_CYCLE_HOURS", 6))

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).parent
SONGS_DIR: Path = BASE_DIR / "songs"
ADS_DIR: Path = BASE_DIR / "ads"
# DJ event clips live under dj_events/<weekday>/ including intro/outro files.
DJ_EVENTS_DIR: Path = BASE_DIR / "dj_events"
DB_PATH: str = str(BASE_DIR / "songs.db")

# Optional branding assets for day-based theme swap.
SUFFERING_BOT_NAME: str = os.getenv("SUFFERING_BOT_NAME", "SufferingFM")
HEAVEN_BOT_NAME: str = os.getenv("HEAVEN_BOT_NAME", "HeavenFM")
SUFFERING_AVATAR_PATH: str = os.getenv("SUFFERING_AVATAR_PATH", "")
HEAVEN_AVATAR_PATH: str = os.getenv("HEAVEN_AVATAR_PATH", "")
SUFFERING_BANNER_PATH: str = os.getenv("SUFFERING_BANNER_PATH", "")
HEAVEN_BANNER_PATH: str = os.getenv("HEAVEN_BANNER_PATH", "")

# ── FFmpeg options ────────────────────────────────────────────────────────────
FFMPEG_BEFORE_OPTIONS: str = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS: dict = {"options": "-vn"}

# Allowed audio extensions for uploads
ALLOWED_EXTENSIONS: tuple = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".webm")
