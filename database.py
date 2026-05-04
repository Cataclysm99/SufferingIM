"""
database.py – SQLite helpers for the songs library.

Schema
------
songs
  id           INTEGER  PRIMARY KEY AUTOINCREMENT
  name         TEXT     NOT NULL
  artist       TEXT     NOT NULL
  added_by     TEXT     NOT NULL DEFAULT ''  -- Discord user ID of the uploader
  filename     TEXT     NOT NULL             -- relative to SONGS_DIR
  times_played INTEGER  NOT NULL DEFAULT 0
  available    INTEGER  NOT NULL DEFAULT 1   -- 1 = active, 0 = deactivated
"""
import sqlite3
from typing import Optional

from config import DB_PATH

# Fields supported by search_songs().
SEARCHABLE_FIELDS = frozenset({"name", "artist", "added_by", "id"})

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
    """Create (or migrate) the songs table."""
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
                available    INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        _migrate(conn)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply schema migrations to an existing database in-place."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(songs)").fetchall()}

    # Rename author → artist (requires SQLite 3.25.0+, released 2018).
    if "author" in cols and "artist" not in cols:
        conn.execute("ALTER TABLE songs RENAME COLUMN author TO artist")
        cols.add("artist")
        cols.discard("author")

    if "added_by" not in cols:
        conn.execute(
            "ALTER TABLE songs ADD COLUMN added_by TEXT NOT NULL DEFAULT ''"
        )

    if "available" not in cols:
        conn.execute(
            "ALTER TABLE songs ADD COLUMN available INTEGER NOT NULL DEFAULT 1"
        )


# ---------------------------------------------------------------------------
# CRUD – read
# ---------------------------------------------------------------------------

def get_all_songs() -> list[dict]:
    """Return all *available* songs ordered by id."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM songs WHERE available = 1 ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_songs_admin() -> list[dict]:
    """Return every song (including deactivated) ordered by id."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM songs ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_song(song_id: int) -> Optional[dict]:
    """Return a single song dict by id regardless of availability, or *None*."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM songs WHERE id = ?", (song_id,)
        ).fetchone()
    return dict(row) if row else None


def get_songs_by_name(name: str) -> list[dict]:
    """Case-insensitive exact name match across *all* songs (inc. deactivated)."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM songs WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


# Pre-built queries for search_songs — avoids any runtime SQL construction.
_FIELD_SEARCH_QUERIES: dict[str, str] = {
    "name": (
        "SELECT * FROM songs WHERE LOWER(name) LIKE LOWER(?) AND available = 1"
    ),
    "artist": (
        "SELECT * FROM songs WHERE LOWER(artist) LIKE LOWER(?) AND available = 1"
    ),
    "added_by": (
        "SELECT * FROM songs WHERE LOWER(added_by) LIKE LOWER(?) AND available = 1"
    ),
}


def search_songs(field: str, query: str) -> list[dict]:
    """
    Case-insensitive substring search across *available* songs.

    *field* must be one of SEARCHABLE_FIELDS.  For the ``"id"`` field the
    query is matched exactly.  Returns an empty list when *field* is
    invalid, the query is malformed, or no matches are found.
    """
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
            sql = _FIELD_SEARCH_QUERIES[field]
            rows = conn.execute(sql, (f"%{query}%",)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# CRUD – write
# ---------------------------------------------------------------------------

def add_song(name: str, artist: str, filename: str, added_by: str = "") -> int:
    """Insert a new song row and return its auto-assigned id."""
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO songs (name, artist, filename, added_by) VALUES (?, ?, ?, ?)",
            (name, artist, filename, added_by),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def deactivate_song(song_id: int) -> Optional[dict]:
    """Set available=0 (keeps DB row and audio file). Returns the song dict or *None*."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM songs WHERE id = ?", (song_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE songs SET available = 0 WHERE id = ?", (song_id,))
        conn.commit()
    return dict(row)


def activate_song(song_id: int) -> Optional[dict]:
    """Set available=1. Returns the song dict or *None* if not found."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM songs WHERE id = ?", (song_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE songs SET available = 1 WHERE id = ?", (song_id,))
        conn.commit()
    return dict(row)


def hard_delete_song(song_id: int) -> Optional[dict]:
    """Remove a song row entirely from the DB. The caller handles the audio file."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM songs WHERE id = ?", (song_id,)
        ).fetchone()
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
