"""
media_utils.py – shared YouTube URL parsing and download helpers.
"""
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
    return _URL_RE.findall(text or "")


def is_youtube_url(url: str) -> bool:
    host = (
        url.split("://", 1)[-1]
        .split("/", 1)[0]
        .split(":", 1)[0]
        .lower()
        .strip()
    )
    return host in _YOUTUBE_HOSTS


def download_youtube_audio(
    url: str,
    target_dir: Path,
    noplaylist: bool = False,
) -> tuple[list[Path], str | None]:
    """Download audio from a YouTube URL.

    Returns a tuple of ``(downloaded_paths, playlist_title)``.  *playlist_title*
    is the YouTube playlist name when the URL points to a playlist, otherwise
    ``None``.
    """
    before = {p.name for p in target_dir.iterdir() if p.is_file()}
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
    for p in sorted(target_dir.iterdir()):
        if not p.is_file():
            continue
        if p.name in before:
            continue
        if p.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        added.append(p)
    return added, playlist_title
