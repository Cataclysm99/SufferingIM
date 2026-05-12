"""
database.py – SQLite helpers for songs, ads, and feedback.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from config import ADS_DIR, DB_PATH

# Fields supported by search_songs().
SEARCHABLE_FIELDS = frozenset({"name", "artist", "added_by", "id"})


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create required tables."""
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS songs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                artist       TEXT    NOT NULL,
                added_by     TEXT    NOT NULL DEFAULT '',
                filename     TEXT    NOT NULL,
                times_played INTEGER NOT NULL DEFAULT 0,
                available    INTEGER NOT NULL DEFAULT 1,
                likes        INTEGER NOT NULL DEFAULT 0,
                dislikes     INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ads (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                filename     TEXT    NOT NULL UNIQUE,
                times_played INTEGER NOT NULL DEFAULT 0,
                available    INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vote_cooldowns (
                user_id      TEXT PRIMARY KEY,
                last_vote_at REAL NOT NULL
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------

def get_all_songs() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM songs WHERE available = 1 ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_songs_admin() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM songs ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_song(song_id: int) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM songs WHERE id = ?", (song_id,)
        ).fetchone()
    return dict(row) if row else None


def get_songs_by_name(name: str) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM songs WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


_FIELD_SEARCH_QUERIES: dict[str, str] = {
    "name": "SELECT * FROM songs WHERE LOWER(name) LIKE LOWER(?) AND available = 1",
    "artist": "SELECT * FROM songs WHERE LOWER(artist) LIKE LOWER(?) AND available = 1",
    "added_by": "SELECT * FROM songs WHERE LOWER(added_by) LIKE LOWER(?) AND available = 1",
}


def search_songs(field: str, query: str) -> list[dict]:
    if field not in SEARCHABLE_FIELDS:
        return []
    with _get_conn() as conn:
        if field == "id":
            try:
                sid = int(query)
            except ValueError:
                return []
            rows = conn.execute(
                "SELECT * FROM songs WHERE id = ? AND available = 1", (sid,)
            ).fetchall()
        else:
            rows = conn.execute(_FIELD_SEARCH_QUERIES[field], (f"%{query}%",)).fetchall()
    return [dict(r) for r in rows]


def add_song(name: str, artist: str, filename: str, added_by: str = "") -> int:
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO songs (name, artist, filename, added_by) VALUES (?, ?, ?, ?)",
            (name, artist, filename, added_by),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def deactivate_song(song_id: int) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE songs SET available = 0 WHERE id = ?", (song_id,))
        conn.commit()
    return dict(row)


def activate_song(song_id: int) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE songs SET available = 1 WHERE id = ?", (song_id,))
        conn.commit()
    return dict(row)


def hard_delete_song(song_id: int) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        conn.commit()
    return dict(row)


def increment_play_count(song_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE songs SET times_played = times_played + 1 WHERE id = ?",
            (song_id,),
        )
        conn.commit()


def get_songs_by_ids(song_ids: list[int]) -> list[dict]:
    if not song_ids:
        return []
    placeholders = ",".join("?" for _ in song_ids)
    sql = (
        f"SELECT * FROM songs WHERE id IN ({placeholders}) AND available = 1 ORDER BY id"
    )
    with _get_conn() as conn:
        rows = conn.execute(sql, tuple(song_ids)).fetchall()
    return [dict(r) for r in rows]


def apply_song_feedback(song_id: int, user_id: int, is_like: bool) -> tuple[bool, str]:
    """Apply like/dislike if user cooldown allows one vote per hour."""
    now = time.time()
    uid = str(user_id)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT last_vote_at FROM vote_cooldowns WHERE user_id = ?",
            (uid,),
        ).fetchone()
        if row is not None:
            elapsed = now - float(row["last_vote_at"])
            if elapsed < 3600:
                minutes = int((3600 - elapsed) // 60) + 1
                return False, f"You can vote again in about {minutes} minute(s)."
            conn.execute(
                "UPDATE vote_cooldowns SET last_vote_at = ? WHERE user_id = ?",
                (now, uid),
            )
        else:
            conn.execute(
                "INSERT INTO vote_cooldowns (user_id, last_vote_at) VALUES (?, ?)",
                (uid, now),
            )

        if is_like:
            conn.execute("UPDATE songs SET likes = likes + 1 WHERE id = ?", (song_id,))
        else:
            conn.execute(
                "UPDATE songs SET dislikes = dislikes + 1 WHERE id = ?",
                (song_id,),
            )
        conn.commit()
    return True, "Vote recorded."


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------

def sync_ads_from_disk() -> None:
    """Ensure every file in ADS_DIR exists in the ads table."""
    ADS_DIR.mkdir(parents=True, exist_ok=True)
    files = [f.name for f in ADS_DIR.iterdir() if f.is_file()]
    if not files:
        return
    with _get_conn() as conn:
        for filename in files:
            conn.execute(
                "INSERT OR IGNORE INTO ads (filename) VALUES (?)",
                (filename,),
            )
        conn.commit()


def get_random_ad() -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ads WHERE available = 1 ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def increment_ad_play_count(ad_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE ads SET times_played = times_played + 1 WHERE id = ?",
            (ad_id,),
        )
        conn.commit()

