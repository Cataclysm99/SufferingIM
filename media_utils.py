"""media_utils.py – shared YouTube URL parsing and download helpers."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import NamedTuple

import yt_dlp
from yt_dlp.utils import DownloadError

from config import (
    ALLOWED_EXTENSIONS,
    YTDLP_AUTH_TEST_URL,
    YTDLP_COOKIE_MODE,
    YTDLP_COOKIES_FILE,
    YTDLP_COOKIES_FROM_BROWSER,
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
_AUTH_COOKIE_NAMES = (
    "SID",
    "HSID",
    "SSID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
)
_COOKIE_MODES = frozenset({"always", "fallback", "off"})
_AUTH_COOKIE_EXPIRY_WARNING_DAYS = 14
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


def _normalized_cookie_mode() -> str:
    """Return the validated cookie usage mode, defaulting invalid values to fallback."""
    mode = YTDLP_COOKIE_MODE.casefold().strip()
    if mode in _COOKIE_MODES:
        return mode
    log.warning(
        "yt-dlp auth: invalid YTDLP_COOKIE_MODE '%s'; expected 'always', 'fallback', "
        "or 'off'. Using 'fallback'.",
        YTDLP_COOKIE_MODE,
    )
    return "fallback"


def _has_cookie_source() -> bool:
    """Return True when any auth source is configured."""
    return bool(YTDLP_OAUTH2 or YTDLP_COOKIES_FROM_BROWSER or YTDLP_COOKIES_FILE)


def _build_ydl_opts(target_dir: Path, noplaylist: bool, include_cookies: bool) -> dict:
    """Build yt-dlp options dict with optional cookies and error-handling settings."""
    opts: dict = {
        "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio/best",
        "noplaylist": noplaylist,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "outtmpl": str(target_dir / "%(title).200B-%(id)s.%(ext)s"),
        "ignoreerrors": True,
    }
    if include_cookies:
        if YTDLP_OAUTH2:
            opts["username"] = "oauth2"
            opts["password"] = ""
        elif YTDLP_COOKIES_FROM_BROWSER:
            opts["cookiesfrombrowser"] = (YTDLP_COOKIES_FROM_BROWSER,)
        elif YTDLP_COOKIES_FILE:
            opts["cookiefile"] = YTDLP_COOKIES_FILE
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


def _expiry_warnings(youtube_rows: list[list[str]], matched: set[str]) -> list[str]:
    """Return human-readable expiry notices for matched auth cookie rows."""
    now = time.time()
    warnings: list[str] = []
    for parts in youtube_rows:
        if parts[5] not in matched:
            continue
        try:
            exp = int(parts[4])
        except (ValueError, IndexError):
            continue
        if exp == 0:
            continue
        days_left = (exp - now) / 86400
        if days_left < 0:
            warnings.append(f"`{parts[5]}` **expired**")
        elif days_left < _AUTH_COOKIE_EXPIRY_WARNING_DAYS:
            warnings.append(f"`{parts[5]}` expires in {int(days_left)}d")
    return warnings


def parse_cookie_file_info(content: str) -> tuple[bool, str, list[str]]:
    """Validate and summarise Netscape cookies file content.

    Returns ``(is_valid, summary_message, auth_cookie_names_found)``.
    *is_valid* is False when the Netscape header is missing or no YouTube
    cookies are present at all.  The summary is always human-readable.
    """
    lines = content.splitlines()
    is_netscape = next(
        (line for line in lines if line.strip()), ""
    ).startswith("# Netscape HTTP Cookie File")
    youtube_rows = [
        parts
        for line in lines
        if line and not line.startswith("#")
        for parts in (line.split("\t"),)
        if len(parts) >= 7 and parts[0].lstrip(".").lower() in _YOUTUBE_HOSTS
    ]
    matched = sorted(
        name for name in _AUTH_COOKIE_NAMES
        if any(p[5] == name for p in youtube_rows)
    )
    if not is_netscape:
        return False, "Missing `# Netscape HTTP Cookie File` header.", []
    if not youtube_rows:
        return False, "No YouTube cookie rows found.", []
    msg_parts = [f"{len(youtube_rows)} YouTube cookie row(s)."]
    if matched:
        msg_parts.append(f"Auth cookies found: `{'`, `'.join(matched)}`.")
    else:
        msg_parts.append("⚠️ No recognised YouTube auth cookies found.")
    expiry = _expiry_warnings(youtube_rows, set(matched))
    if expiry:
        msg_parts.append("⚠️ Expiry notice: " + "; ".join(expiry) + ".")
    return True, " ".join(msg_parts), matched


def _cookie_file_diagnostics() -> tuple[bool, str]:
    """Return a startup status message for the configured cookie file."""
    cookie_path = Path(YTDLP_COOKIES_FILE).expanduser()
    if not cookie_path.exists():
        return False, f"yt-dlp auth: cookie file not found: {cookie_path}"
    if not cookie_path.is_file():
        return False, f"yt-dlp auth: cookie path is not a file: {cookie_path}"
    try:
        content = cookie_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"yt-dlp auth: could not read cookie file {cookie_path}: {exc}"
    is_valid, summary, _ = parse_cookie_file_info(content)
    prefix = f"yt-dlp auth: cookie file {cookie_path}: "
    return is_valid, prefix + summary


def log_ytdlp_auth_diagnostics() -> None:
    """Log yt-dlp authentication startup diagnostics."""
    cookie_mode = _normalized_cookie_mode()
    has_cookie_source = _has_cookie_source()
    log.info("yt-dlp auth: cookie mode '%s'.", cookie_mode)

    if cookie_mode == "off":
        if has_cookie_source:
            log.info("yt-dlp auth: cookie source configured but disabled by mode 'off'.")
        else:
            log.info("yt-dlp auth: no auth source configured.")
        return

    if YTDLP_OAUTH2:
        log.info(
            "yt-dlp auth: OAuth2 mode enabled (yt-dlp-youtube-oauth2 plugin). "
            "Ensure the plugin is installed and the one-time device-code setup has been run."
        )
    elif YTDLP_COOKIES_FROM_BROWSER:
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

    ydl_opts = _build_ydl_opts(Path.cwd(), True, include_cookies=True)
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


def _is_rate_limited_error(exc: DownloadError) -> bool:
    """Return True when the yt-dlp error text looks like a rate-limit failure."""
    error_text = str(exc).casefold()
    return any(hint in error_text for hint in _RATE_LIMIT_ERROR_HINTS)


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


def _extract_with_cookie_strategy(
    url: str,
    target_dir: Path,
    noplaylist: bool,
    cookie_mode: str,
    has_cookie_source: bool,
) -> _ExtractAttemptResult:
    """Extract with configured cookie mode and fallback retry behavior."""
    use_cookies = cookie_mode == "always" and has_cookie_source
    first_attempt_logs = _YTDLPLogCapture()
    first_opts = _build_ydl_opts(target_dir, noplaylist, include_cookies=use_cookies)
    first_opts["logger"] = first_attempt_logs

    try:
        playlist_title, skipped_titles = _extract_download_info(url, first_opts)
        should_retry_with_cookies = (
            cookie_mode == "fallback"
            and has_cookie_source
            and _captured_auth_gated_errors(first_attempt_logs.errors)
        )
        if not should_retry_with_cookies:
            return _ExtractAttemptResult(
                playlist_title,
                skipped_titles,
                first_attempt_logs.errors.copy(),
            )
        log.info("yt-dlp download retrying with cookies after auth-gated skips: %s", url)
        retry_logs = _YTDLPLogCapture()
        retry_opts = _build_ydl_opts(target_dir, noplaylist, include_cookies=True)
        retry_opts["logger"] = retry_logs
        try:
            playlist_title, skipped_titles = _extract_download_info(url, retry_opts)
            return _ExtractAttemptResult(
                playlist_title,
                skipped_titles,
                retry_logs.errors.copy(),
            )
        except DownloadError as retry_exc:
            _log_captured_failures(url, "initial attempt", first_attempt_logs.errors, retry_exc)
            _log_captured_failures(url, "cookie retry", retry_logs.errors, retry_exc)
            raise
    except DownloadError as first_exc:
        should_retry_with_cookies = (
            cookie_mode == "fallback"
            and has_cookie_source
            and _is_auth_gated_error(first_exc)
        )
        if not should_retry_with_cookies:
            _log_captured_failures(url, "initial attempt", first_attempt_logs.errors, first_exc)
            raise
        log.info("yt-dlp download retrying with cookies after auth-gated failure: %s", url)
        retry_logs = _YTDLPLogCapture()
        retry_opts = _build_ydl_opts(target_dir, noplaylist, include_cookies=True)
        retry_opts["logger"] = retry_logs
        try:
            playlist_title, skipped_titles = _extract_download_info(url, retry_opts)
            return _ExtractAttemptResult(
                playlist_title,
                skipped_titles,
                retry_logs.errors.copy(),
            )
        except DownloadError as retry_exc:
            _log_captured_failures(url, "initial attempt", first_attempt_logs.errors, first_exc)
            _log_captured_failures(url, "cookie retry", retry_logs.errors, retry_exc)
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
    cookie_mode = _normalized_cookie_mode()
    has_cookie_source = _has_cookie_source()
    extraction = _extract_with_cookie_strategy(
        url=url,
        target_dir=target_dir,
        noplaylist=noplaylist,
        cookie_mode=cookie_mode,
        has_cookie_source=has_cookie_source,
    )
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
