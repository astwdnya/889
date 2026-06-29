"""
hohoj_handler.py
----------------
استخراج لینک دانلود از hohoj.tv (پلیر hls.js داخل iframe).

روش کار:
  1. صفحه‌ی /video?id=<id> رو می‌گیریم → عنوان + لینک iframe embed
  2. صفحه‌ی /embed?id=<id> رو می‌گیریم
  3. لینک m3u8 در inline script (hls.loadSource) قرار داره:
       https://video-N.ggjav.com/.../index.m3u8
  4. دانلود با yt-dlp (HLS)

نکته: لینک m3u8 توکن زمان‌دار نداره (پایدارتر از RedTube).
"""

import asyncio
import html as html_lib
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs

logger = logging.getLogger("HohojHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

_SITE_DOMAIN = "hohoj.tv"
_SITE_URL = "https://hohoj.tv"
_SITE_REFERER = f"{_SITE_URL}/"

_ALLOWED_HOSTS = frozenset(
    {
        "hohoj.tv",
        "www.hohoj.tv",
    }
)

# دامنه‌های CDN رسانه (m3u8 و ts)
_ALLOWED_HOST_SUFFIXES = (
    ".hohoj.tv",
    ".ggjav.com",
)

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_hohoj_url(url: str) -> bool:
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


def _extract_video_id(url: str) -> Optional[str]:
    try:
        qs = parse_qs(urlparse(url).query)
        vid = qs.get("id", [None])[0]
        if vid and vid.isdigit():
            return vid
    except Exception:
        pass
    return None


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
        logger.debug("fetch failed: %s", e)
        return None, 0


def _extract_title(html: str, fallback: str = "Untitled") -> str:
    m = re.search(
        r'<meta[^>]+og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
    )
    if m:
        return html_lib.unescape(m.group(1).strip()) or fallback
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*hohoj.*$", "", title, flags=re.I).strip()
        return html_lib.unescape(title) or fallback
    return fallback


def _extract_m3u8(html: str) -> Optional[str]:
    """لینک m3u8 رو از inline script (hls.loadSource) استخراج می‌کنه."""
    # 1. لینک کامل m3u8
    m = re.search(r'(https?://[^"\'\\\s]+?\.m3u8[^"\'\\\s]*)', html)
    if m:
        return m.group(1)
    # 2. fallback: hls.loadSource("...")
    m = re.search(r'loadSource\(\s*["\']([^"\']+)["\']', html)
    if m:
        return m.group(1)
    return None


# ─── Main extraction ───────────────────────────────────────


async def extract_hohoj_qualities(url: str) -> Tuple[List[dict], str]:
    """لینک m3u8 رو از hohoj.tv استخراج می‌کنه."""
    if not is_hohoj_url(url):
        return [], "Invalid URL"

    if not _check_impersonation_support():
        return [], "curl_cffi لازمه: pip install curl_cffi"

    video_id = _extract_video_id(url)
    if not video_id:
        return [], "Could not parse video id from URL"

    # 1. صفحه‌ی اصلی برای عنوان
    page_url = f"{_SITE_URL}/video?id={video_id}"
    logger.info("Fetching page: %s", page_url)
    page_html, status = await _fetch(page_url, _SITE_REFERER)
    if not page_html:
        return [], f"Could not fetch page (HTTP {status})"

    title = _extract_title(page_html)

    # 2. صفحه‌ی embed برای لینک ویدیو
    embed_url = f"{_SITE_URL}/embed?id={video_id}"
    logger.info("Fetching embed: %s", embed_url)
    embed_html, estatus = await _fetch(embed_url, page_url)
    if not embed_html:
        return [], f"Could not fetch embed page (HTTP {estatus})"

    # 3. استخراج لینک m3u8
    m3u8_url = _extract_m3u8(embed_html)
    if not m3u8_url:
        return [], "m3u8 source not found in embed page"

    m3u8_url = urljoin(embed_url, m3u8_url)

    if not _is_allowed_host(m3u8_url):
        logger.debug("m3u8 host not allowed: %s", m3u8_url[:60])
        return [], "m3u8 host not allowed"

    # لینک منفرده (variants داخل خودش)؛ yt-dlp بهترین کیفیت رو می‌گیره
    qualities = [
        {
            "label": "📡 Auto (HLS)",
            "url": m3u8_url,
            "method": "m3u8",
        }
    ]
    logger.info("Extracted m3u8 for: %s", title[:60])
    return qualities, title


# ─── Download (yt-dlp) ──────────────────────────────────────


async def _download_with_ytdlp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    await progress_cb("📥 **شروع دانلود (yt-dlp)...**")
    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--progress",
            "--newline",
            "--no-check-certificates",
            "--max-filesize",
            str(MAX_DOWNLOAD_SIZE),
            "--add-header",
            f"Referer:{_SITE_REFERER}",
            "--add-header",
            f"User-Agent:{_USER_AGENT}",
            "--merge-output-format",
            "mp4",
            "-o",
            filepath,
        ]
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


async def download_hohoj_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود HLS stream از hohoj با yt-dlp."""
    if not _is_allowed_host(m3u8_url):
        return False, "URL host not allowed", 0

    success, error, size = await _download_with_ytdlp(m3u8_url, filepath, progress_cb)
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error, 0


async def download_hohoj_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود مستقیم (همون yt-dlp - برای سازگاری با API دیگر handlerها)."""
    return await download_hohoj_m3u8(url, filepath, progress_cb)
