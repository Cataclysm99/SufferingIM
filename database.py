"""database.py – SQLite helpers for songs, ads, and feedback."""

from __future__ import annotations

import sqlite3
import time
import re
from hashlib import sha256
from pathlib import Path
from typing import Iterable, Optional

from config import ADS_DIR, ALLOWED_EXTENSIONS, DB_PATH, DJ_EVENTS_DIR, SONGS_DIR

SEARCHABLE_FIELDS = frozenset({"name", "artist", "added_by", "genre", "id"})

GENRE_FILTER_MODE_ALL = "all"
GENRE_FILTER_MODE_INCLUDE = "include"
GENRE_FILTER_MODE_EXCLUDE = "exclude"


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


def normalize_genre_name(raw_genre: str) -> str:
    """Normalize one genre label for storage and comparisons."""
    return " ".join(raw_genre.strip().lower().split())


def _normalize_search_text(raw_text: str) -> str:
    """Normalize search text so separators like underscores behave like spaces."""
    return " ".join(re.sub(r"[\W_]+", " ", raw_text.casefold()).split())


def _extract_artist_from_song_name(song_name: str) -> str:
    """Extract an artist-like prefix from titles split by dash separators."""
    parts = re.split(r"(?:_+-+_+|\s+-+\s+|\s+[–—]+\s+)", song_name, maxsplit=1)
    if len(parts) < 2:
        return ""
    return _normalize_search_text(parts[0])


def parse_genre_names(raw_genres: str | None) -> list[str]:
    """Parse a comma-separated genre string into a unique normalized list."""
    if not raw_genres:
        return []
    seen: set[str] = set()
    parsed: list[str] = []
    for chunk in str(raw_genres).split(","):
        genre = normalize_genre_name(chunk)
        if genre and genre not in seen:
            seen.add(genre)
            parsed.append(genre)
    return parsed


def serialize_genre_names(genres: Iterable[str] | str | None) -> str:
    """Serialize genres into the canonical comma-separated storage format."""
    if isinstance(genres, str) or genres is None:
        parsed = parse_genre_names(genres)
    else:
        parsed = parse_genre_names(",".join(str(genre) for genre in genres))
    return ", ".join(parsed)


def init_db() -> None:
    """Create required tables."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS songs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                artist       TEXT    NOT NULL,
                genres       TEXT    NOT NULL DEFAULT '',
                added_by     TEXT    NOT NULL DEFAULT '',
                filename     TEXT    NOT NULL,
                times_played INTEGER NOT NULL DEFAULT 0,
                available    INTEGER NOT NULL DEFAULT 1,
                vote_score   INTEGER NOT NULL DEFAULT 0
            )
            """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ads (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL DEFAULT '',
                sponsor      TEXT    NOT NULL DEFAULT 'Unknown',
                added_by     TEXT    NOT NULL DEFAULT '',
                filename     TEXT    NOT NULL UNIQUE,
                times_played INTEGER NOT NULL DEFAULT 0,
                available    INTEGER NOT NULL DEFAULT 1
            )
            """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                sponsor      TEXT    NOT NULL DEFAULT 'Unknown',
                added_by     TEXT    NOT NULL DEFAULT '',
                day          TEXT    NOT NULL,
                slot         TEXT    NOT NULL DEFAULT 'event',
                filename     TEXT    NOT NULL,
                times_played INTEGER NOT NULL DEFAULT 0,
                available    INTEGER NOT NULL DEFAULT 1,
                UNIQUE(day, slot, filename)
            )
            """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vote_cooldowns (
                user_id  TEXT    NOT NULL,
                song_id  INTEGER NOT NULL,
                voted_at REAL    NOT NULL,
                PRIMARY KEY (user_id)
            )
            """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS genre_filter_state (
                id   INTEGER PRIMARY KEY CHECK (id = 1),
                mode TEXT    NOT NULL DEFAULT 'all'
            )
            """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS genre_filter_entries (
                genre TEXT PRIMARY KEY
            )
            """)
        conn.execute(
            """
            INSERT INTO genre_filter_state (id, mode)
            VALUES (1, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (GENRE_FILTER_MODE_ALL,),
        )
        _ensure_column(conn, "songs", "genres", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "songs", "content_hash", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ads", "name", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "ads", "sponsor", "TEXT NOT NULL DEFAULT 'Unknown'")
        _ensure_column(conn, "ads", "added_by", "TEXT NOT NULL DEFAULT ''")
        _normalize_song_genres(conn)
        _backfill_song_content_hashes(conn)
        _ensure_vote_cooldown_user_uniqueness(conn)
        _purge_reserved_media_rows(conn)
        conn.commit()


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    """Add a column when the target table exists but the column is missing."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {str(row["name"]) for row in rows}
    if column in existing:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_vote_cooldown_user_uniqueness(conn: sqlite3.Connection) -> None:
    """Keep only one cooldown row per user and enforce that uniqueness."""
    conn.execute(
        """
        DELETE FROM vote_cooldowns
        WHERE rowid NOT IN (
            SELECT MAX(rowid)
            FROM vote_cooldowns
            GROUP BY user_id
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_vote_cooldowns_user_id
        ON vote_cooldowns(user_id)
        """
    )


def _purge_reserved_media_rows(conn: sqlite3.Connection) -> None:
    """Delete placeholder/hidden media filenames from all media tables."""
    reserved_filter = "TRIM(filename) = '' OR LTRIM(filename) LIKE '.%'"
    for table in ("songs", "ads", "broadcasts"):
        conn.execute(f"DELETE FROM {table} WHERE {reserved_filter}")


def _normalize_song_genres(conn: sqlite3.Connection) -> None:
    """Normalize stored song genre strings after schema upgrades."""
    rows = conn.execute("SELECT id, genres FROM songs").fetchall()
    for row in rows:
        serialized = serialize_genre_names(row["genres"])
        if serialized == row["genres"]:
            continue
        conn.execute(
            "UPDATE songs SET genres = ? WHERE id = ?",
            (serialized, row["id"]),
        )


def _file_content_hash(path: Path) -> str:
    """Return a deterministic SHA-256 hash for a file."""
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backfill_song_content_hashes(conn: sqlite3.Connection) -> None:
    """Populate missing song content_hash values for existing files."""
    rows = conn.execute(
        "SELECT id, filename FROM songs WHERE COALESCE(content_hash, '') = ''"
    ).fetchall()
    for row in rows:
        filename = str(row["filename"]).strip()
        if not filename:
            continue
        path = SONGS_DIR / filename
        if not path.is_file():
            continue
        song_hash = _file_content_hash(path)
        duplicate = conn.execute(
            """
            SELECT id
            FROM songs
            WHERE id != ? AND content_hash = ?
            LIMIT 1
            """,
            (row["id"], song_hash),
        ).fetchone()
        if duplicate is not None:
            continue
        conn.execute(
            "UPDATE songs SET content_hash = ? WHERE id = ?",
            (song_hash, row["id"]),
        )


def _get_genre_filter_state(conn: sqlite3.Connection) -> tuple[str, set[str]]:
    """Return the current daily genre-filter mode and tracked genres."""
    row = conn.execute("SELECT mode FROM genre_filter_state WHERE id = 1").fetchone()
    mode = str(row["mode"]) if row else GENRE_FILTER_MODE_ALL
    entries = conn.execute(
        "SELECT genre FROM genre_filter_entries ORDER BY genre"
    ).fetchall()
    return mode, {str(entry["genre"]) for entry in entries}


def _set_genre_filter_state(
    conn: sqlite3.Connection,
    mode: str,
    genres: Iterable[str],
) -> None:
    """Persist the current daily genre-filter state."""
    normalized = parse_genre_names(",".join(genres))
    conn.execute(
        """
        INSERT INTO genre_filter_state (id, mode)
        VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET mode = excluded.mode
        """,
        (mode,),
    )
    conn.execute("DELETE FROM genre_filter_entries")
    conn.executemany(
        "INSERT INTO genre_filter_entries (genre) VALUES (?)",
        [(genre,) for genre in normalized],
    )


def get_daily_genre_filter() -> dict:
    """Return the current daily genre-filter mode and genres."""
    with _get_conn() as conn:
        mode, genres = _get_genre_filter_state(conn)
    return {"mode": mode, "genres": sorted(genres)}


def _song_matches_daily_genres(song: dict, mode: str, tracked_genres: set[str]) -> bool:
    """Return whether a song remains playable under the current daily filter."""
    if mode == GENRE_FILTER_MODE_ALL or not tracked_genres:
        return True
    song_genres = set(parse_genre_names(song.get("genres", "")))
    if mode == GENRE_FILTER_MODE_INCLUDE:
        return bool(song_genres & tracked_genres)
    if not song_genres:
        return True
    return not song_genres.issubset(tracked_genres)


def _apply_daily_genre_filter(songs: list[dict]) -> list[dict]:
    """Filter songs according to the current daily genre settings."""
    with _get_conn() as conn:
        mode, tracked_genres = _get_genre_filter_state(conn)
    return [
        song
        for song in songs
        if _song_matches_daily_genres(song, mode, tracked_genres)
    ]


# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------


def get_all_songs() -> list[dict]:
    """Return all active songs ordered by ID."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM songs WHERE available = 1 ORDER BY id"
        ).fetchall()
    return _apply_daily_genre_filter([dict(row) for row in rows])


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
    """Search songs by supported field name. ID queries include disabled songs."""
    if field not in SEARCHABLE_FIELDS:
        return []
    normalized_query = _normalize_search_text(query)
    results: list[dict] | None = None
    if field == "genre":
        target_genre = normalize_genre_name(query)
        if not target_genre:
            return []
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM songs WHERE available = 1 ORDER BY id"
            ).fetchall()
        results = [
            dict(row)
            for row in rows
            if target_genre in parse_genre_names(row["genres"])
        ]
    elif field == "artist":
        if not normalized_query:
            return []
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM songs WHERE available = 1 ORDER BY id"
            ).fetchall()
        results = [
            dict(row)
            for row in rows
            if normalized_query in _normalize_search_text(str(row["artist"]))
            or normalized_query in _extract_artist_from_song_name(str(row["name"]))
        ]
    else:
        with _get_conn() as conn:
            if field == "id":
                try:
                    song_id = int(query)
                except ValueError:
                    return []
                rows = conn.execute(
                    "SELECT * FROM songs WHERE id = ?",
                    (song_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    _FIELD_SEARCH_QUERIES[field], (f"%{query}%",)
                ).fetchall()
        results = [dict(row) for row in rows]
    return results


def add_song(
    name: str,
    artist: str,
    filename: str,
    added_by: str = "",
    metadata: dict | None = None,
) -> int | None:
    """Insert a new song and return its ID, or None when it is an active duplicate."""
    if _is_reserved_media_filename(filename):
        raise ValueError(f"Reserved media filename is not allowed: {filename}")
    metadata = metadata or {}
    content_hash = str(metadata.get("content_hash", "")).strip()
    genres_metadata = metadata.get("genres")
    genres_value = (
        serialize_genre_names(str(genres_metadata))
        if genres_metadata is not None
        else None
    )
    if not content_hash:
        path = SONGS_DIR / filename
        if path.is_file():
            content_hash = _file_content_hash(path)
    with _get_conn() as conn:
        if content_hash:
            duplicate = conn.execute(
                "SELECT id, available FROM songs WHERE content_hash = ? LIMIT 1",
                (content_hash,),
            ).fetchone()
            if duplicate is not None:
                duplicate_id = int(duplicate["id"])
                if int(duplicate["available"]) == 0:
                    if genres_value is None:
                        conn.execute(
                            """
                            UPDATE songs
                            SET name = ?,
                                artist = ?,
                                filename = ?,
                                added_by = ?,
                                available = 1
                            WHERE id = ?
                            """,
                            (name, artist, filename, added_by, duplicate_id),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE songs
                            SET name = ?,
                                artist = ?,
                                genres = ?,
                                filename = ?,
                                added_by = ?,
                                available = 1
                            WHERE id = ?
                            """,
                            (
                                name,
                                artist,
                                genres_value,
                                filename,
                                added_by,
                                duplicate_id,
                            ),
                        )
                    conn.commit()
                    return duplicate_id
                return None
        cursor = conn.execute(
            (
                "INSERT INTO songs (name, artist, genres, filename, added_by, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (
                name,
                artist,
                genres_value or "",
                filename,
                added_by,
                content_hash,
            ),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]


def update_song_metadata(
    song_id: int,
    updates: dict,
    added_by: str,
) -> Optional[dict]:
    """Update editable song metadata and return the refreshed row."""
    name = str(updates.get("name", "")).strip()
    genres = serialize_genre_names(str(updates.get("genres", "")))
    available = bool(updates.get("available", True))
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE songs
            SET name = ?,
                genres = ?,
                available = ?,
                added_by = ?
            WHERE id = ?
            """,
            (
                name,
                serialize_genre_names(genres),
                1 if available else 0,
                added_by,
                song_id,
            ),
        )
        conn.commit()
        refreshed = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
    return dict(refreshed) if refreshed else None


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
    """Apply feedback with a one-hour per-user cooldown."""
    now = time.time()
    uid = str(user_id)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT voted_at FROM vote_cooldowns WHERE user_id = ?",
            (uid,),
        ).fetchone()
        if row is not None:
            elapsed = now - float(row["voted_at"])
            if elapsed < 3600:
                minutes = int((3600 - elapsed) // 60) + 1
                return (
                    False,
                    "You've already given feedback recently. "
                    f"Try again in about {minutes} minute(s).",
                )
        conn.execute(
            """
            INSERT INTO vote_cooldowns (user_id, song_id, voted_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                song_id = excluded.song_id,
                voted_at = excluded.voted_at
            """,
            (uid, song_id, now),
        )

        delta = -1 if is_like else 1
        conn.execute(
            "UPDATE songs SET vote_score = vote_score + ? WHERE id = ?",
            (delta, song_id),
        )
        conn.commit()
    return (
        True,
        "Thanks for your opinion. I've put it in a special place just for you. :wastebasket:",
    )


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


def enable_daily_genres(genres: str | Iterable[str]) -> dict:
    """Enable genres for the day, either exclusively or by re-enabling disabled ones."""
    requested = parse_genre_names(genres if isinstance(genres, str) else ",".join(genres))
    if not requested:
        return get_daily_genre_filter()
    requested_set = set(requested)
    with _get_conn() as conn:
        mode, tracked_genres = _get_genre_filter_state(conn)
        if mode == GENRE_FILTER_MODE_EXCLUDE and requested_set.issubset(tracked_genres):
            remaining = tracked_genres - requested_set
            next_mode = GENRE_FILTER_MODE_EXCLUDE if remaining else GENRE_FILTER_MODE_ALL
            _set_genre_filter_state(conn, next_mode, remaining)
        else:
            _set_genre_filter_state(conn, GENRE_FILTER_MODE_INCLUDE, requested)
        conn.commit()
    return get_daily_genre_filter()


def disable_daily_genres(genres: str | Iterable[str]) -> dict:
    """Disable genres for the day while keeping all other genres active."""
    requested = parse_genre_names(genres if isinstance(genres, str) else ",".join(genres))
    if not requested:
        return get_daily_genre_filter()
    requested_set = set(requested)
    with _get_conn() as conn:
        mode, tracked_genres = _get_genre_filter_state(conn)
        if mode == GENRE_FILTER_MODE_INCLUDE:
            _set_genre_filter_state(
                conn,
                GENRE_FILTER_MODE_INCLUDE,
                tracked_genres - requested_set,
            )
        elif mode == GENRE_FILTER_MODE_EXCLUDE:
            _set_genre_filter_state(
                conn,
                GENRE_FILTER_MODE_EXCLUDE,
                tracked_genres | requested_set,
            )
        else:
            _set_genre_filter_state(conn, GENRE_FILTER_MODE_EXCLUDE, requested)
        conn.commit()
    return get_daily_genre_filter()


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------


def add_ad(name: str, sponsor: str, filename: str, added_by: str = "") -> int:
    """Insert a new ad and return its generated database ID."""
    if _is_reserved_media_filename(filename):
        raise ValueError(f"Reserved media filename is not allowed: {filename}")
    with _get_conn() as conn:
        cursor = conn.execute(
            (
                "INSERT INTO ads (name, sponsor, added_by, filename) "
                "VALUES (?, ?, ?, ?)"
            ),
            (name, sponsor, added_by, filename),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]


def get_all_ads_admin() -> list[dict]:
    """Return all ad records ordered by ID."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM ads ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def sync_ads_from_disk() -> None:
    """Ensure every file in ADS_DIR exists in the ads table."""
    ADS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(
        path.name
        for path in ADS_DIR.iterdir()
        if path.is_file() and not _is_reserved_media_filename(path.name)
    )
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM ads WHERE TRIM(filename) = '' OR LTRIM(filename) LIKE '.%'"
        )
        for filename in files:
            stem = Path(filename).stem
            conn.execute(
                (
                    "INSERT INTO ads (name, sponsor, added_by, filename) "
                    "VALUES (?, ?, '', ?) "
                    "ON CONFLICT(filename) DO UPDATE SET "
                    "name = COALESCE(NULLIF(ads.name, ''), excluded.name), "
                    "sponsor = COALESCE(NULLIF(ads.sponsor, ''), 'Unknown')"
                ),
                (stem, "Unknown", filename),
            )
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
        conn.execute(
            "UPDATE ads SET times_played = times_played + 1 WHERE id = ?", (ad_id,)
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Broadcast clips
# ---------------------------------------------------------------------------


def _infer_broadcast_slot(filename: str) -> str:
    """Infer broadcast slot from filename stem."""
    stem = Path(filename).stem.lower()
    if stem == "intro":
        return "intro"
    if stem == "outro":
        return "outro"
    return "event"


def _normalized_day(day: str) -> str:
    """Return a normalized weekday identifier."""
    return day.strip().lower()


def add_broadcast(record: dict) -> int:
    """Insert a new broadcast clip row and return its generated database ID."""
    filename = str(record.get("filename", ""))
    if _is_reserved_media_filename(filename):
        raise ValueError(f"Reserved media filename is not allowed: {filename}")
    with _get_conn() as conn:
        cursor = conn.execute(
            (
                "INSERT INTO broadcasts (name, sponsor, added_by, day, slot, filename) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (
                str(record.get("name", "")).strip() or Path(filename).stem,
                str(record.get("sponsor", "")).strip() or "Unknown",
                str(record.get("added_by", "")),
                _normalized_day(str(record.get("day", ""))),
                str(record.get("slot", "event")).strip().lower() or "event",
                filename,
            ),
        )
        conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]


def get_all_broadcasts_admin() -> list[dict]:
    """Return all broadcast clips ordered by day, slot, then id."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM broadcasts ORDER BY day, slot, id"
        ).fetchall()
    return [dict(row) for row in rows]


def sync_broadcasts_from_disk() -> None:
    """Ensure all DJ event files are represented in the broadcasts table."""
    DJ_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM broadcasts WHERE TRIM(filename) = '' OR LTRIM(filename) LIKE '.%'"
        )
        for day_dir in sorted(DJ_EVENTS_DIR.iterdir(), key=lambda path: path.name):
            if not day_dir.is_dir() or day_dir.name.startswith("."):
                continue
            day = _normalized_day(day_dir.name)
            for path in sorted(day_dir.iterdir(), key=lambda file_path: file_path.name):
                if (
                    not path.is_file()
                    or _is_reserved_media_filename(path.name)
                    or path.suffix.lower() not in ALLOWED_EXTENSIONS
                ):
                    continue
                slot = _infer_broadcast_slot(path.name)
                conn.execute(
                    (
                        "INSERT INTO broadcasts (name, sponsor, added_by, day, slot, filename) "
                        "VALUES (?, ?, '', ?, ?, ?) "
                        "ON CONFLICT(day, slot, filename) DO UPDATE SET "
                        "name = COALESCE(NULLIF(broadcasts.name, ''), excluded.name), "
                        "sponsor = COALESCE(NULLIF(broadcasts.sponsor, ''), 'Unknown')"
                    ),
                    (path.stem, "Unknown", day, slot, path.name),
                )
        conn.commit()


def get_broadcast_clip(day: str, slot: str) -> Optional[dict]:
    """Return one available broadcast clip for the selected day and slot."""
    normalized_day = _normalized_day(day)
    normalized_slot = slot.strip().lower()
    with _get_conn() as conn:
        if normalized_slot == "event":
            row = conn.execute(
                (
                    "SELECT * FROM broadcasts "
                    "WHERE day = ? AND slot = ? AND available = 1 "
                    "ORDER BY RANDOM() LIMIT 1"
                ),
                (normalized_day, normalized_slot),
            ).fetchone()
        else:
            row = conn.execute(
                (
                    "SELECT * FROM broadcasts "
                    "WHERE day = ? AND slot = ? AND available = 1 "
                    "ORDER BY id LIMIT 1"
                ),
                (normalized_day, normalized_slot),
            ).fetchone()
    return dict(row) if row else None


def get_broadcast_by_id(broadcast_id: int) -> Optional[dict]:
    """Return one broadcast clip by ID when it is available."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM broadcasts WHERE id = ? AND available = 1",
            (broadcast_id,),
        ).fetchone()
    return dict(row) if row else None


def increment_broadcast_play_count(broadcast_id: int) -> None:
    """Increment the play count for a broadcast clip."""
    with _get_conn() as conn:
        conn.execute(
            (
                "UPDATE broadcasts SET times_played = times_played + 1 "
                "WHERE id = ?"
            ),
            (broadcast_id,),
        )
        conn.commit()
