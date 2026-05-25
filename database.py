"""database.py – SQLite helpers for songs, ads, and feedback."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from config import ADS_DIR, DB_PATH

SEARCHABLE_FIELDS = frozenset({"name", "artist", "added_by", "id"})


def _get_conn() -> sqlite3.Connection:
    """Open a SQLite connection configured to return rows by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _is_reserved_media_filename(filename: str) -> bool:
    """Return True for placeholder or hidden filenames that should never enter the DB."""
    name = Path(filename).name.strip()
    if not name:
        return True
    return name.startswith(".")


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
                vote_score   INTEGER NOT NULL DEFAULT 0
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
                user_id  TEXT    NOT NULL,
                song_id  INTEGER NOT NULL,
                voted_at REAL    NOT NULL,
                PRIMARY KEY (user_id, song_id)
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------

def get_all_songs() -> list[dict]:
    """Return all active songs ordered by ID."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM songs WHERE available = 1 ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def get_all_songs_admin() -> list[dict]:
    """Return all songs, including deactivated entries, ordered by ID."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM songs ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def get_disabled_song_filenames() -> set[str]:
    """Return filenames for songs currently marked unavailable."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT filename FROM songs WHERE available = 0").fetchall()
    return {str(row["filename"]) for row in rows if row["filename"]}


def get_song(song_id: int) -> Optional[dict]:
    """Return one song by ID, or None when it does not exist."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
    return dict(row) if row else None


def get_songs_by_name(name: str) -> list[dict]:
    """Return every song whose name matches the given value case-insensitively."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM songs WHERE LOWER(name) = LOWER(?)",
            (name,),
        ).fetchall()
    return [dict(row) for row in rows]


_FIELD_SEARCH_QUERIES: dict[str, str] = {
    "name": "SELECT * FROM songs WHERE LOWER(name) LIKE LOWER(?) AND available = 1",
    "artist": "SELECT * FROM songs WHERE LOWER(artist) LIKE LOWER(?) AND available = 1",
    "added_by": "SELECT * FROM songs WHERE LOWER(added_by) LIKE LOWER(?) AND available = 1",
}


def search_songs(field: str, query: str) -> list[dict]:
    """Search active songs by supported field name."""
    if field not in SEARCHABLE_FIELDS:
        return []
    with _get_conn() as conn:
        if field == "id":
            try:
                song_id = int(query)
            except ValueError:
                return []
            rows = conn.execute(
                "SELECT * FROM songs WHERE id = ? AND available = 1",
                (song_id,),
            ).fetchall()
        else:
            rows = conn.execute(_FIELD_SEARCH_QUERIES[field], (f"%{query}%",)).fetchall()
    return [dict(row) for row in rows]


def add_song(name: str, artist: str, filename: str, added_by: str = "") -> int:
    """Insert a new song and return its generated database ID."""
    if _is_reserved_media_filename(filename):
        raise ValueError(f"Reserved media filename is not allowed: {filename}")
    with _get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO songs (name, artist, filename, added_by) VALUES (?, ?, ?, ?)",
            (name, artist, filename, added_by),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]


def deactivate_song(song_id: int) -> Optional[dict]:
    """Mark a song unavailable and return its previous row."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE songs SET available = 0 WHERE id = ?", (song_id,))
        conn.commit()
    return dict(row)


def activate_song(song_id: int) -> Optional[dict]:
    """Mark a song available and return its previous row."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE songs SET available = 1 WHERE id = ?", (song_id,))
        conn.commit()
    return dict(row)


def hard_delete_song(song_id: int) -> Optional[dict]:
    """Remove a song row entirely and return the deleted row."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM songs WHERE id = ?", (song_id,))
        conn.commit()
    return dict(row)


def purge_all_songs() -> list[dict]:
    """Delete every song record from the database and return the deleted records."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM songs").fetchall()
        conn.execute("DELETE FROM songs")
        conn.commit()
    return [dict(row) for row in rows]


def increment_play_count(song_id: int) -> None:
    """Increment the play count for a song."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE songs SET times_played = times_played + 1 WHERE id = ?",
            (song_id,),
        )
        conn.commit()


def get_songs_by_ids(song_ids: list[int]) -> list[dict]:
    """Return active songs matching the provided IDs."""
    if not song_ids:
        return []
    placeholders = ",".join("?" for _ in song_ids)
    sql = f"SELECT * FROM songs WHERE id IN ({placeholders}) AND available = 1 ORDER BY id"
    with _get_conn() as conn:
        rows = conn.execute(sql, tuple(song_ids)).fetchall()
    return [dict(row) for row in rows]


def apply_song_feedback(song_id: int, user_id: int, is_like: bool) -> tuple[bool, str]:
    """Apply per-song feedback with a one-hour per-user cooldown."""
    now = time.time()
    uid = str(user_id)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT voted_at FROM vote_cooldowns WHERE user_id = ? AND song_id = ?",
            (uid, song_id),
        ).fetchone()
        if row is not None:
            elapsed = now - float(row["voted_at"])
            if elapsed < 3600:
                minutes = int((3600 - elapsed) // 60) + 1
                return (
                    False,
                    "You've already given feedback for this song. "
                    f"Try again in about {minutes} minute(s).",
                )
            conn.execute(
                "UPDATE vote_cooldowns SET voted_at = ? WHERE user_id = ? AND song_id = ?",
                (now, uid, song_id),
            )
        else:
            conn.execute(
                "INSERT INTO vote_cooldowns (user_id, song_id, voted_at) VALUES (?, ?, ?)",
                (uid, song_id, now),
            )

        delta = -1 if is_like else 1
        conn.execute(
            "UPDATE songs SET vote_score = vote_score + ? WHERE id = ?",
            (delta, song_id),
        )
        conn.commit()
    return True, "Feedback received."


def reset_song_vote_score(song_id: int) -> None:
    """Reset a song's vote score after it plays."""
    with _get_conn() as conn:
        conn.execute("UPDATE songs SET vote_score = 0 WHERE id = ?", (song_id,))
        conn.commit()


def reset_negative_vote_scores() -> None:
    """Reset all negative vote scores to zero at the start of a new DJ cycle."""
    with _get_conn() as conn:
        conn.execute("UPDATE songs SET vote_score = 0 WHERE vote_score < 0")
        conn.commit()


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------

def sync_ads_from_disk() -> None:
    """Ensure every file in ADS_DIR exists in the ads table."""
    ADS_DIR.mkdir(parents=True, exist_ok=True)
    files = [
        path.name
        for path in ADS_DIR.iterdir()
        if path.is_file() and not _is_reserved_media_filename(path.name)
    ]
    if not files:
        return
    with _get_conn() as conn:
        for filename in files:
            conn.execute("INSERT OR IGNORE INTO ads (filename) VALUES (?)", (filename,))
        conn.commit()


def get_random_ad() -> Optional[dict]:
    """Return a random available ad row, if one exists."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ads WHERE available = 1 ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def increment_ad_play_count(ad_id: int) -> None:
    """Increment the play count for an ad."""
    with _get_conn() as conn:
        conn.execute("UPDATE ads SET times_played = times_played + 1 WHERE id = ?", (ad_id,))
        conn.commit()
