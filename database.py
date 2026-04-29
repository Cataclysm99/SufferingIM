"""
database.py – SQLite helpers for the songs library.

Schema
------
songs
  id           INTEGER  PRIMARY KEY AUTOINCREMENT
  name         TEXT     NOT NULL
  author       TEXT     NOT NULL
  filename     TEXT     NOT NULL   -- relative to SONGS_DIR
  times_played INTEGER  DEFAULT 0
"""
import sqlite3
from typing import Optional

from config import DB_PATH

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create the songs table if it does not already exist."""
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS songs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                author       TEXT    NOT NULL,
                filename     TEXT    NOT NULL,
                times_played INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def get_all_songs() -> list[dict]:
    """Return every song ordered by id."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM songs ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_song(song_id: int) -> Optional[dict]:
    """Return a single song dict, or *None* if not found."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
    return dict(row) if row else None


def add_song(name: str, author: str, filename: str) -> int:
    """Insert a new song row and return its auto-assigned id."""
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO songs (name, author, filename) VALUES (?, ?, ?)",
            (name, author, filename),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def delete_song(song_id: int) -> Optional[dict]:
    """Delete a song by id and return the deleted row dict, or *None*."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        conn.commit()
    return dict(row)


def increment_play_count(song_id: int) -> None:
    """Atomically increment the times_played counter for a song."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE songs SET times_played = times_played + 1 WHERE id = ?",
            (song_id,),
        )
        conn.commit()
