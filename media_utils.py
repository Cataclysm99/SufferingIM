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
    YTDLP_OAUTH2,
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
_AUTH_GATED_ERROR_HINTS = (
    "private video",
    "this video is private",
    "login required",
    "sign in",
    "age-restricted",
    "members-only",
    "members only",
)
_RATE_LIMIT_ERROR_HINTS = (
    "current session has been rate-limited",
    "rate-limited by youtube",
)
_YTDLP_ERROR_DETAIL_RE = re.compile(
    r"ERROR:\s*\[[^\]]+\]\s*([A-Za-z0-9_-]{6,}):\s*(.+)$",
    re.IGNORECASE,
)
_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

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


class _ExtractAttemptResult(NamedTuple):
    """Result of yt-dlp extraction attempts before filesystem scan."""

    playlist_title: str | None
    skipped_titles: list[str]
    final_attempt_errors: list[str]


def _build_ydl_opts(target_dir: Path, noplaylist: bool, include_auth: bool) -> dict:
    """Build yt-dlp options dict with optional OAuth2 auth and error-handling settings."""
    opts: dict = {
        "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio/best",
        "noplaylist": noplaylist,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "outtmpl": str(target_dir / "%(title).200B-%(id)s.%(ext)s"),
        "ignoreerrors": True,
    }
    if include_auth and YTDLP_OAUTH2:
        opts["username"] = "oauth2"
        opts["password"] = ""
    return opts


class _YTDLPLogCapture:
    """Capture yt-dlp error lines so callers can defer logging until retries are exhausted."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    @staticmethod
    def debug(message: str, *args: object) -> None:
        """Ignore debug output from yt-dlp."""
        del message, args

    @staticmethod
    def warning(message: str, *args: object) -> None:
        """Ignore warning output from yt-dlp."""
        del message, args

    def error(self, message: str, *args: object) -> None:
        """Capture error output from yt-dlp."""
        if args:
            try:
                message = message % args
            except (TypeError, ValueError):
                message = f"{message} {' '.join(str(arg) for arg in args)}"
        self.errors.append(str(message))


def _log_captured_failures(
    url: str,
    attempt_label: str,
    captured_errors: list[str],
    fallback_error: DownloadError,
) -> None:
    """Log captured yt-dlp errors for one attempt, or fallback to the raised exception."""
    if captured_errors:
        for error_text in captured_errors:
            log.warning("yt-dlp %s failed for %s: %s", attempt_label, url, error_text)
        return
    log.warning("yt-dlp %s failed for %s: %s", attempt_label, url, fallback_error)


def log_ytdlp_auth_diagnostics() -> None:
    """Log yt-dlp authentication startup diagnostics."""
    if YTDLP_OAUTH2:
        log.info(
            "yt-dlp auth: OAuth2 enabled (yt-dlp-youtube-oauth2 plugin). "
            "Anonymous download is tried first; OAuth2 is used as a fallback for "
            "private, age-restricted, or members-only content."
        )
    else:
        log.info(
            "yt-dlp auth: OAuth2 not configured. Only publicly accessible content "
            "can be downloaded. Set YTDLP_OAUTH2=true to enable auth fallback."
        )

    if not YTDLP_AUTH_TEST_URL:
        log.info(
            "yt-dlp auth: startup age-check skipped; set YTDLP_AUTH_TEST_URL "
            "to an age-restricted YouTube URL to verify authenticated access.",
        )
        return

    ydl_opts = _build_ydl_opts(Path.cwd(), True, include_auth=True)
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


def _extract_download_info(url: str, ydl_opts: dict) -> tuple[str | None, list[str]]:
    """Run yt-dlp extraction and return playlist title plus unavailable-entry labels."""
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
    return playlist_title, skipped_titles


def _error_text_is_auth_gated(error_text: str) -> bool:
    """Return True when an error message indicates auth-protected content."""
    folded = error_text.casefold()
    if any(hint in folded for hint in _RATE_LIMIT_ERROR_HINTS):
        return False
    return any(hint in folded for hint in _AUTH_GATED_ERROR_HINTS)


def _captured_auth_gated_errors(captured_errors: list[str]) -> bool:
    """Return True when captured yt-dlp errors contain auth-gated failures."""
    return any(_error_text_is_auth_gated(error_text) for error_text in captured_errors)


def _format_skipped_from_error(error_text: str) -> str:
    """Format a yt-dlp error line into a user-facing skipped-track label."""
    cleaned = error_text.strip()
    parsed = _YTDLP_ERROR_DETAIL_RE.search(cleaned)
    if not parsed:
        return cleaned
    video_id, reason = parsed.groups()
    summary = f"{video_id} — {reason.strip()}"
    if _YOUTUBE_VIDEO_ID_RE.fullmatch(video_id):
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        return f"{summary} ({watch_url})"
    return summary


def _enrich_skipped_titles(skipped_titles: list[str], captured_errors: list[str]) -> list[str]:
    """Merge skipped titles with detailed yt-dlp errors, preferring detailed entries."""
    enriched: list[str] = []
    seen: set[str] = set()
    for error_text in captured_errors:
        label = _format_skipped_from_error(error_text)
        if label in seen:
            continue
        enriched.append(label)
        seen.add(label)
    for title in skipped_titles:
        if title == "(unknown)" or title in seen:
            continue
        enriched.append(title)
        seen.add(title)
    if enriched:
        return enriched
    return skipped_titles


def _is_auth_gated_error(exc: DownloadError) -> bool:
    """Return True when the yt-dlp error text indicates auth-protected content."""
    return _error_text_is_auth_gated(str(exc))


def _extract_with_auth_fallback(
    url: str,
    target_dir: Path,
    noplaylist: bool,
) -> _ExtractAttemptResult:
    """Try an anonymous download; retry with OAuth2 for auth-gated failures."""
    anon_logs = _YTDLPLogCapture()
    anon_opts = _build_ydl_opts(target_dir, noplaylist, include_auth=False)
    anon_opts["logger"] = anon_logs

    try:
        playlist_title, skipped_titles = _extract_download_info(url, anon_opts)
        should_retry = YTDLP_OAUTH2 and _captured_auth_gated_errors(anon_logs.errors)
        if not should_retry:
            return _ExtractAttemptResult(playlist_title, skipped_titles, anon_logs.errors.copy())
        log.info("yt-dlp download retrying with OAuth2 after auth-gated skips: %s", url)
        retry_logs = _YTDLPLogCapture()
        retry_opts = _build_ydl_opts(target_dir, noplaylist, include_auth=True)
        retry_opts["logger"] = retry_logs
        try:
            playlist_title, skipped_titles = _extract_download_info(url, retry_opts)
            return _ExtractAttemptResult(playlist_title, skipped_titles, retry_logs.errors.copy())
        except DownloadError as retry_exc:
            _log_captured_failures(url, "anonymous attempt", anon_logs.errors, retry_exc)
            _log_captured_failures(url, "OAuth2 retry", retry_logs.errors, retry_exc)
            raise
    except DownloadError as anon_exc:
        should_retry = YTDLP_OAUTH2 and _is_auth_gated_error(anon_exc)
        if not should_retry:
            _log_captured_failures(url, "anonymous attempt", anon_logs.errors, anon_exc)
            raise
        log.info("yt-dlp download retrying with OAuth2 after auth-gated failure: %s", url)
        retry_logs = _YTDLPLogCapture()
        retry_opts = _build_ydl_opts(target_dir, noplaylist, include_auth=True)
        retry_opts["logger"] = retry_logs
        try:
            playlist_title, skipped_titles = _extract_download_info(url, retry_opts)
            return _ExtractAttemptResult(playlist_title, skipped_titles, retry_logs.errors.copy())
        except DownloadError as retry_exc:
            _log_captured_failures(url, "anonymous attempt", anon_logs.errors, anon_exc)
            _log_captured_failures(url, "OAuth2 retry", retry_logs.errors, retry_exc)
            raise


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
    extraction = _extract_with_auth_fallback(url, target_dir, noplaylist)
    playlist_title = extraction.playlist_title
    skipped_titles = _enrich_skipped_titles(
        extraction.skipped_titles,
        extraction.final_attempt_errors,
    )

    added: list[Path] = []
    for path in sorted(target_dir.iterdir()):
        if not path.is_file() or path.name in before:
            continue
        if path.suffix.lower() in ALLOWED_EXTENSIONS:
            added.append(path)
    if not added and skipped_titles:
        if extraction.final_attempt_errors:
            for error_text in extraction.final_attempt_errors:
                log.warning(
                    "yt-dlp skipped-only result for %s: %s",
                    url,
                    error_text,
                )
        else:
            log.warning("yt-dlp skipped-only result for %s: %s", url, ", ".join(skipped_titles))
    return DownloadResult(added, playlist_title, skipped_titles)
