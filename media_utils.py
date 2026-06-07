"""media_utils.py – shared YouTube URL parsing and download helpers."""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import yt_dlp

from config import ALLOWED_EXTENSIONS, YTDLP_COOKIES_FILE, YTDLP_COOKIES_FROM_BROWSER

_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


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
