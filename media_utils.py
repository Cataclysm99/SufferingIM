"""media_utils.py – shared YouTube URL parsing and download helpers."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

import yt_dlp
from yt_dlp.utils import DownloadError

from config import (
    ALLOWED_EXTENSIONS,
    YTDLP_AUTH_TEST_URL,
    YTDLP_COOKIES_FILE,
    YTDLP_COOKIES_FROM_BROWSER,
)

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_AUTH_COOKIE_NAMES = (
    "SID",
    "HSID",
    "SSID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
)

log = logging.getLogger(__name__)


def extract_urls(text: str) -> list[str]:
    """Extract URL-like substrings from free-form text."""
    return _URL_RE.findall(text or "")


def is_youtube_url(url: str) -> bool:
    """Return True when the URL host belongs to YouTube."""
    host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower().strip()
    return host in _YOUTUBE_HOSTS


class DownloadResult(NamedTuple):
    """Result of a YouTube download operation."""

    downloaded: list[Path]
    playlist_title: str | None
    skipped_titles: list[str]


def _build_ydl_opts(target_dir: Path, noplaylist: bool) -> dict:
    """Build yt-dlp options dict, including cookies and error-handling settings."""
    opts: dict = {
        "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio/best",
        "noplaylist": noplaylist,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "outtmpl": str(target_dir / "%(title).200B-%(id)s.%(ext)s"),
        "ignoreerrors": True,
    }
    if YTDLP_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (YTDLP_COOKIES_FROM_BROWSER,)
    elif YTDLP_COOKIES_FILE:
        opts["cookiefile"] = YTDLP_COOKIES_FILE
    return opts


def _cookie_file_diagnostics() -> tuple[bool, str]:
    """Return a startup status message for the configured cookie file."""
    cookie_path = Path(YTDLP_COOKIES_FILE).expanduser()
    if not cookie_path.exists():
        return False, f"yt-dlp auth: cookie file not found: {cookie_path}"
    if not cookie_path.is_file():
        return False, f"yt-dlp auth: cookie path is not a file: {cookie_path}"
    try:
        lines = cookie_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return False, f"yt-dlp auth: could not read cookie file {cookie_path}: {exc}"

    cookie_rows = [line for line in lines if line and not line.startswith("#")]
    cookie_entries = [row.split("\t") for row in cookie_rows]
    youtube_rows = [
        parts
        for parts in cookie_entries
        if len(parts) >= 7 and parts[0].lstrip(".").lower() in _YOUTUBE_HOSTS
    ]
    cookie_names = {
        parts[5]
        for parts in youtube_rows
    }
    matched_names = sorted(name for name in _AUTH_COOKIE_NAMES if name in cookie_names)
    details = (
        f"yt-dlp auth: cookie file readable: {cookie_path} "
        f"({len(youtube_rows)} youtube cookie row(s)"
    )
    if matched_names:
        joined_names = ", ".join(matched_names)
        return True, f"{details}; auth cookies: {joined_names})"
    return True, f"{details}; auth cookies not detected)"


def log_ytdlp_auth_diagnostics() -> None:
    """Log yt-dlp authentication startup diagnostics."""
    if YTDLP_COOKIES_FROM_BROWSER:
        log.info(
            "yt-dlp auth: using live browser cookies from '%s'.",
            YTDLP_COOKIES_FROM_BROWSER,
        )
        if YTDLP_COOKIES_FILE:
            log.info(
                "yt-dlp auth: browser-cookie mode overrides cookie file '%s'.",
                YTDLP_COOKIES_FILE,
            )
    elif YTDLP_COOKIES_FILE:
        cookies_ok, cookie_message = _cookie_file_diagnostics()
        if cookies_ok:
            log.info(cookie_message)
        else:
            log.warning(cookie_message)
    else:
        log.info("yt-dlp auth: no cookie source configured.")
        return

    if not YTDLP_AUTH_TEST_URL:
        log.info(
            "yt-dlp auth: startup age-check skipped; set YTDLP_AUTH_TEST_URL "
            "to an age-restricted YouTube URL to verify authenticated access.",
        )
        return

    ydl_opts = _build_ydl_opts(Path.cwd(), True)
    ydl_opts["skip_download"] = True
    ydl_opts["simulate"] = True
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(YTDLP_AUTH_TEST_URL, download=False)
    except (DownloadError, OSError, ValueError) as exc:
        log.warning(
            "yt-dlp auth: startup age-check failed for %s: %s",
            YTDLP_AUTH_TEST_URL,
            exc,
        )
        return
    log.info("yt-dlp auth: startup age-check passed for %s.", YTDLP_AUTH_TEST_URL)


def download_youtube_audio(
    url: str,
    target_dir: Path,
    noplaylist: bool = False,
) -> DownloadResult:
    """Download audio from a YouTube URL.

    Returns a :class:`DownloadResult` containing:
    - *downloaded*: paths to newly-downloaded files whose extension is allowed.
    - *playlist_title*: the playlist title when the URL is a playlist.
    - *skipped_titles*: display titles (or IDs) of entries that were unavailable.
    """
    before = {path.name for path in target_dir.iterdir() if path.is_file()}
    ydl_opts = _build_ydl_opts(target_dir, noplaylist)

    playlist_title: str | None = None
    skipped_titles: list[str] = []

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url)
        if isinstance(info, dict):
            if info.get("_type") == "playlist":
                playlist_title = info.get("title") or None
                for entry in info.get("entries", []):
                    if entry is None:
                        skipped_titles.append("(unknown)")
                    elif entry.get("_type") == "ERROR" or not entry.get("id"):
                        skipped_titles.append(
                            entry.get("title") or entry.get("id") or "(unknown)"
                        )

    added: list[Path] = []
    for path in sorted(target_dir.iterdir()):
        if not path.is_file() or path.name in before:
            continue
        if path.suffix.lower() in ALLOWED_EXTENSIONS:
            added.append(path)
    return DownloadResult(added, playlist_title, skipped_titles)
