"""
porn300_handler.py
------------------
استخراج و دانلود از porn300.com.

روش کار:
  1. صفحه‌ی ویدیو رو با curl_cffi می‌گیریم
  2. لینک MP4 مستقیم با توکن (?secure=<sig>,<time>) رو از <source> یا
     data-video استخراج می‌کنیم
  3. URL-decode می‌کنیم (== و , انکود شده‌ان: %3D%3D%2C)
  4. دانلود مستقیم با yt-dlp

نکته: توکن secure زمان‌داره → extract باید بلافاصله قبل از دانلود صدا زده بشه.
"""

import asyncio
import html as html_lib
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

logger = logging.getLogger("Porn300Handler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

_SITE_DOMAIN = "porn300.com"
_SITE_URL = "https://www.porn300.com"
_SITE_REFERER = f"{_SITE_URL}/"

_ALLOWED_HOSTS = frozenset({"porn300.com", "www.porn300.com"})

# دامنه‌های CDN رسانه
_ALLOWED_HOST_SUFFIXES = (
    ".porn300.com",
)

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_porn300_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(f".{_SITE_DOMAIN}")
    except Exception:
        return False


def _is_allowed_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or any(
            host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES
        )
    except Exception:
        return False


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _check_impersonation_support() -> bool:
    try:
        import curl_cffi  # noqa: F401

        return True
    except ImportError:
        return False


def _find_output_file(filepath: str) -> Optional[str]:
    if os.path.exists(filepath):
        return filepath
    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = base + ext
        if os.path.exists(candidate):
            try:
                os.rename(candidate, filepath)
                return filepath
            except OSError:
                return candidate
    return None


def _clean_url(raw: str) -> str:
    """تمیز کردن لینک: unescape HTML و URL-decode توکن (%3D%3D%2C → ==,)."""
    raw = html_lib.unescape(raw.strip())
    return unquote(raw)


def _extract_title(html: str) -> str:
    m = re.search(
        r'<meta[^>]+og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
    )
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*Porn300.*$", "", title, flags=re.I).strip()
        return html_lib.unescape(title) or "Untitled"
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*Porn300.*$", "", title, flags=re.I).strip()
        return html_lib.unescape(title) or "Untitled"
    return "Untitled"


# ─── HTTP (curl_cffi) ───────────────────────────────────────


async def _fetch(url: str, referer: str) -> Tuple[Optional[str], int]:
    if not _check_impersonation_support():
        return None, 0
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession() as session:
            try:
                await session.get(_SITE_REFERER, impersonate="chrome", timeout=15)
            except Exception:
                pass
            resp = await session.get(
                url,
                impersonate="chrome",
                headers={"Referer": referer},
                timeout=25,
            )
            return resp.text, resp.status_code
    except Exception as e:
        logger.warning("fetch failed: %s", e)
        return None, 0


# ─── Main extraction ───────────────────────────────────────


def _find_video_url(html: str) -> Optional[str]:
    """لینک MP4 مستقیم با توکن secure رو پیدا می‌کنه."""
    # 1. از <source src="...mp4?secure=...">
    m = re.search(
        r'<source[^>]+src=["\']'
        r'(https?://cdnst\d*\.porn300\.com[^"\']+?\.mp4\?secure=[^"\']+)',
        html,
        re.I,
    )
    if m:
        return _clean_url(m.group(1))

    # 2. از data-video="...mp4?secure=..." (پلیر موبایل)
    m = re.search(
        r'data-video=["\']'
        r'(https?://cdnst\d*\.porn300\.com[^"\']+?\.mp4\?secure=[^"\']+)',
        html,
        re.I,
    )
    if m:
        return _clean_url(m.group(1))

    # 3. fallback: هر لینک mp4 با secure روی cdnst
    m = re.search(
        r'(https?://cdnst\d*\.porn300\.com[^"\'\\\s]+?\.mp4\?secure=[^"\'\\\s]+)',
        html,
    )
    if m:
        return _clean_url(m.group(1))
    return None


async def extract_porn300_qualities(url: str) -> Tuple[List[dict], str]:
    """لینک ویدیو رو از porn300 استخراج می‌کنه."""
    if not is_porn300_url(url):
        return [], "Invalid URL"

    if not _check_impersonation_support():
        return [], "curl_cffi لازمه: pip install curl_cffi"

    logger.info("Fetching page: %s", url)
    html, status = await _fetch(url, _SITE_REFERER)
    if not html:
        return [], f"Could not fetch page (HTTP {status})"

    title = _extract_title(html)
    video_url = _find_video_url(html)

    if not video_url:
        return [], "لینک ویدیو پیدا نشد (ساختار سایت تغییر کرده؟)"

    if not _is_allowed_host(video_url):
        logger.warning("Video host not allowed: %s", video_url[:60])
        return [], "میزبان لینک مجاز نیست"

    qualities = [
        {
            "label": "🎬 دانلود (MP4)",
            "url": video_url,
            "method": "direct",
            "page_url": url,
        }
    ]
    logger.info("Extracted video for: %s", title[:60])
    return qualities, title


# ─── Download (yt-dlp) ──────────────────────────────────────


async def _download_with_ytdlp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    has_aria2c = shutil.which("aria2c") is not None
    mode = "aria2c" if has_aria2c else "concurrent x16"
    await progress_cb(f"📥 **شروع دانلود (yt-dlp · {mode})...**")

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--progress",
            "--newline",
            "--no-check-certificates",
            "--concurrent-fragments",
            "16",
            "--retries",
            "5",
            "--fragment-retries",
            "5",
            "--retry-sleep",
            "fragment:linear=1:5:2",
            "--buffer-size",
            "16K",
            "--max-filesize",
            str(MAX_DOWNLOAD_SIZE),
            "--add-header",
            f"Referer:{_SITE_REFERER}",
            "--add-header",
            f"Origin:{_SITE_URL}",
            "--add-header",
            f"User-Agent:{_USER_AGENT}",
            "--merge-output-format",
            "mp4",
            "-o",
            filepath,
        ]

        if has_aria2c:
            cmd.extend(
                [
                    "--downloader",
                    "aria2c",
                    "--downloader-args",
                    "aria2c:-x16 -s16 -k1M --max-connection-per-server=16 "
                    "--min-split-size=1M --console-log-level=warn",
                ]
            )

        if _check_impersonation_support():
            cmd.extend(["--impersonate", "chrome"])
        cmd.append(url)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        last_update = 0.0
        tail: List[str] = []
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=180)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                _cleanup_file(filepath)
                return False, "Download timed out", 0
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                tail.append(text)
                if len(tail) > 15:
                    tail.pop(0)
            now = time.time()
            if now - last_update >= 2.0 and text:
                last_update = now
                await progress_cb(f"📥 **Downloading...**\n`{text[:80]}`")

        await process.wait()
        if process.returncode != 0:
            full_err = "\n".join(tail).lower()
            if any(p in full_err for p in ("404", "403", "forbidden", "expired")):
                return False, "__EXPIRED__", 0
            err = "\n".join(tail[-5:]) or "yt-dlp failed"
            return False, err[:200], 0

        actual_path = _find_output_file(filepath)
        if not actual_path:
            return False, "Output file not found", 0

        size = os.path.getsize(actual_path)
        if size == 0:
            _cleanup_file(actual_path)
            return False, "Downloaded file is empty", 0
        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(actual_path)
            return False, "File exceeds size limit", 0
        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        return False, str(e)[:150], 0


# ─── Download: Public API ──────────────────────────────────


async def download_porn300_m3u8(
    media_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    page_url: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود از porn300 با yt-dlp.

    اگه توکن منقضی شده باشه و page_url داده شده باشه، یک بار خودکار
    دوباره extract می‌کنه و با لینک تازه تلاش می‌کنه.
    """
    if not _is_allowed_host(media_url):
        return False, "URL host not allowed", 0

    success, error, size = await _download_with_ytdlp(media_url, filepath, progress_cb)
    if success:
        return True, "", size

    _cleanup_file(filepath)

    # ── توکن منقضی: یک بار رفرش خودکار ──
    if error == "__EXPIRED__" and page_url and is_porn300_url(page_url):
        await progress_cb("♻️ **توکن منقضی شد، در حال گرفتن لینک تازه...**")
        qualities, _title = await extract_porn300_qualities(page_url)
        if qualities:
            fresh_url = qualities[0]["url"]
            success, error, size = await _download_with_ytdlp(
                fresh_url, filepath, progress_cb
            )
            if success:
                return True, "", size
            _cleanup_file(filepath)

    if error == "__EXPIRED__":
        error = "لینک منقضی شد. لطفاً دوباره لینک ویدیو رو بفرست."

    return False, error, 0


async def download_porn300_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    page_url: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """دانلود مستقیم (برای سازگاری با API دیگر handlerها)."""
    return await download_porn300_m3u8(url, filepath, progress_cb, page_url)
