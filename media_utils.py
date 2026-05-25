"""media_utils.py – shared YouTube URL parsing and download helpers."""
from __future__ import annotations

import re
from pathlib import Path

import yt_dlp

from config import ALLOWED_EXTENSIONS

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


def download_youtube_audio(
    url: str,
    target_dir: Path,
    noplaylist: bool = False,
) -> tuple[list[Path], str | None]:
    """Download audio from a YouTube URL and return downloaded files plus playlist title."""
    before = {path.name for path in target_dir.iterdir() if path.is_file()}
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": noplaylist,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "outtmpl": str(target_dir / "%(title).200B-%(id)s.%(ext)s"),
    }
    playlist_title: str | None = None
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url)
        if isinstance(info, dict) and info.get("_type") == "playlist":
            playlist_title = info.get("title") or None

    added: list[Path] = []
    for path in sorted(target_dir.iterdir()):
        if not path.is_file() or path.name in before:
            continue
        if path.suffix.lower() in ALLOWED_EXTENSIONS:
            added.append(path)
    return added, playlist_title
