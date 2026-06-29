"""
tube8_handler.py
----------------
استخراج لینک‌های دانلود از tube8.com (پلیر MindGeek/Aylo).

روش کار:
  1. صفحه‌ی ویدیو رو می‌گیریم و mediaDefinitions رو پیدا می‌کنیم
  2. لینک 'media/hls/?s=<token>' یه JSON با لیست کیفیت‌ها برمی‌گردونه
  3. هر کیفیت یه master.m3u8 جدا داره (240/480/720/1080)
  4. دانلود با yt-dlp (HLS)
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("Tube8Handler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

_SITE_DOMAIN = "tube8.com"
_SITE_URL = "https://www.tube8.com"
_SITE_REFERER = f"{_SITE_URL}/"

_ALLOWED_HOSTS = frozenset(
    {
        "tube8.com",
        "www.tube8.com",
    }
)

_ALLOWED_HOST_SUFFIXES = (
    ".tube8.com",
    ".t8cdn.com",
)

ProgressCallback = Callable[[str], Awaitable[None]]

tube8_sessions: dict = {}


# ─── Utility ────────────────────────────────────────────────


def is_tube8_url(url: str) -> bool:
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


def _quality_sort_key(q: dict) -> int:
    nums = re.findall(r"\d+", q["label"])
    return int(nums[-1]) if nums else 0


# ─── HTTP (curl_cffi) ───────────────────────────────────────


async def _fetch(url: str, referer: str) -> Tuple[Optional[str], int]:
    """دریافت محتوا با curl_cffi (برای دور زدن محافظت)."""
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


def _extract_title(html: str) -> str:
    m = re.search(
        r'<meta[^>]+og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
    )
    if m:
        return m.group(1).strip()
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*Tube8.*$", "", title, flags=re.I).strip()
        title = re.sub(r"\s*Porn Videos?\s*$", "", title, flags=re.I).strip()
        return title or "Untitled"
    return "Untitled"


# ─── Main extraction ───────────────────────────────────────


async def extract_tube8_qualities(url: str) -> Tuple[List[dict], str]:
    """لینک‌های کیفیت مختلف رو از Tube8 استخراج میکنه."""
    if not is_tube8_url(url):
        return [], "Invalid URL"

    if not _check_impersonation_support():
        return [], "curl_cffi لازمه: pip install curl_cffi"

    logger.info("Fetching page: %s", url)
    html, status = await _fetch(url, _SITE_REFERER)
    if not html:
        return [], f"Could not fetch page (HTTP {status})"

    title = _extract_title(html)

    # 1. پیدا کردن لینک media/hls/?s= از mediaDefinitions
    hls_token_url = None
    for m in re.finditer(r'"format":"hls","videoUrl":"([^"]+)"', html):
        candidate = m.group(1).replace("\\/", "/")
        if "/media/hls/" in candidate:
            hls_token_url = candidate
            break

    if not hls_token_url:
        # fallback: هر media/hls/?s=
        m = re.search(r'"videoUrl":"([^"]*/media/hls/[^"]+)"', html)
        if m:
            hls_token_url = m.group(1).replace("\\/", "/")

    if not hls_token_url:
        return [], "HLS source not found in mediaDefinitions"

    # 2. گرفتن JSON کیفیت‌ها از media/hls/?s=
    logger.info("Fetching quality list: %s", hls_token_url[:80])
    quality_json, qstatus = await _fetch(hls_token_url, url)
    if not quality_json:
        return [], f"Could not fetch quality list (HTTP {qstatus})"

    try:
        defs = json.loads(quality_json)
    except json.JSONDecodeError:
        return [], "quality list returned invalid JSON"

    qualities: List[dict] = []
    seen = set()

    for d in defs:
        if not isinstance(d, dict):
            continue
        video_url = (d.get("videoUrl") or "").replace("\\/", "/")
        quality = d.get("quality") or ""
        if not video_url or video_url in seen:
            continue
        if not _is_allowed_host(video_url):
            logger.debug("Skipping non-allowed host: %s", video_url[:60])
            continue
        seen.add(video_url)

        label = f"{quality}p" if quality else "Auto"
        qualities.append(
            {
                "label": f"📡 {label}",
                "url": video_url,
                "method": "m3u8",
            }
        )

    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info("Extracted %d qualities for: %s", len(qualities), title[:60])
        return qualities, title

    return [], "no playable qualities found"


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
            "10",
            "--fragment-retries",
            "10",
            "--retry-sleep",
            "fragment:exp=1:30",
            "--buffer-size",
            "16K",
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
            stderr=asyncio.subprocess.PIPE,
        )
        last_update = 0.0
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
            now = time.time()
            if now - last_update >= 2.0 and text:
                last_update = now
                await progress_cb(f"📥 **Downloading...**\n`{text[:80]}`")

        await process.wait()
        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            return False, stderr[:200], 0

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


async def download_tube8_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود HLS stream از Tube8 با yt-dlp."""
    if not _is_allowed_host(m3u8_url):
        return False, "URL host not allowed", 0

    success, error, size = await _download_with_ytdlp(m3u8_url, filepath, progress_cb)
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error, 0


async def download_tube8_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود مستقیم (همون yt-dlp - برای سازگاری با API دیگر handlerها)."""
    return await download_tube8_m3u8(url, filepath, progress_cb)
