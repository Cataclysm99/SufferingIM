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


# Channel where the persistent controller message is (re-)posted on startup.
# Leave empty / 0 to skip auto-posting.
CONTROLLER_CHANNEL_ID: int = _int_env("CONTROLLER_CHANNEL_ID", 0)

# ── Permissions ───────────────────────────────────────────────────────────────
# Discord role ID whose members may add, delete, and toggle songs.
# Set to 0 (or omit) to allow everyone to manage the library (unrestricted mode).
MUSIC_MANAGER_ROLE_ID: int = _int_env("MUSIC_MANAGER_ROLE_ID", 0)

# ── Rigged song ───────────────────────────────────────────────────────────────
# Database row-id of the song that is secretly inserted into the queue.
# Set to 0 to disable the rigged feature entirely.
RIGGED_SONG_ID: int = _int_env("RIGGED_SONG_ID", 0)

# Odds: 1-in-RIGGED_CHANCE probability of playing the rigged song next.
RIGGED_CHANCE: int = 10

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR: Path = Path(__file__).parent
SONGS_DIR: Path = BASE_DIR / "songs"
DB_PATH: str = str(BASE_DIR / "songs.db")

# ── FFmpeg options ────────────────────────────────────────────────────────────
FFMPEG_BEFORE_OPTIONS: str = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS: dict = {"options": "-vn"}

# Allowed audio extensions for uploads
ALLOWED_EXTENSIONS: tuple = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus")
