"""
porntrex_handler.py
───────────────────
دانلود ویدیو از porntrex.com (KVS Player)

پیش‌نیاز: pip install aiohttp aiofiles
"""

import asyncio
import logging
import os
import re
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("PorntrexHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_DOWNLOAD_SIZE = 4 * 1024 * 1024 * 1024  # 4 GB
MIN_FILE_SIZE = 1024
CHUNK_SIZE = 512 * 1024  # 512 KB
PROGRESS_INTERVAL = 2.0
MAX_RETRIES = 3
RETRY_DELAY = 2.0

_ALLOWED_HOSTS = frozenset({
    "porntrex.com",
    "www.porntrex.com",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_porntrex_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS
    except Exception:
        return False


def _cleanup_file(filepath: str) -> None:
    try:
        os.remove(filepath)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Cleanup failed %s: %s", filepath, e)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


# ─── Extraction ─────────────────────────────────────────────


async def _fetch_page(url: str) -> Optional[str]:
    """دریافت HTML صفحه (بدون نیاز به Playwright)."""
    timeout = ClientTimeout(total=30, connect=10)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, headers=_DEFAULT_HEADERS
            ) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    logger.warning("HTTP %d for %s", resp.status, url)
                    if 400 <= resp.status < 500:
                        return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Fetch attempt %d: %s", attempt, e)

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None


def _parse_flashvars(html: str) -> dict:
    """
    استخراج flashvars از HTML.

    KVS Player فرمت:
        var flashvars = {
            video_url: 'https://...',
            video_alt_url: 'https://...',
            ...
        };
    """
    # پیدا کردن بلاک flashvars
    m = re.search(
        r'var\s+flashvars\s*=\s*\{([^;]+)\};',
        html,
        re.S,
    )
    if not m:
        return {}

    block = m.group(1)
    result = {}

    # parse key: 'value' و key: "value"
    for km in re.finditer(
        r"(\w+)\s*:\s*['\"]([^'\"]*)['\"]",
        block,
    ):
        result[km.group(1)] = km.group(2)

    # parse key: number
    for km in re.finditer(
        r"(\w+)\s*:\s*(\d+(?:\.\d+)?)\s*[,}]",
        block,
    ):
        key = km.group(1)
        if key not in result:
            result[key] = km.group(2)

    return result


def _extract_title(html: str) -> str:
    """استخراج عنوان."""
    # og:title
    m = re.search(
        r'property=["\']og:title["\']\s+content=["\']([^"\']+)',
        html, re.I,
    )
    if m:
        return m.group(1).strip()

    # <title>
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        # حذف " | Porntrex.com"
        title = re.sub(
            r"\s*[|]\s*Porntrex\.com.*$", "", title, flags=re.I
        ).strip()
        if title:
            return title

    return "Untitled"


def _extract_info(html: str, flashvars: dict) -> dict:
    """استخراج اطلاعات اضافی."""
    info = {}

    info["video_id"] = flashvars.get("video_id", "")
    info["title"] = flashvars.get("video_title", "")
    info["categories"] = flashvars.get("video_categories", "")
    info["tags"] = flashvars.get("video_tags", "")

    # duration
    m = re.search(r'"duration"\s*:\s*"([^"]+)"', html)
    if m:
        info["duration"] = m.group(1)

    # thumbnail
    preview = flashvars.get("preview_url", "")
    if preview:
        if preview.startswith("//"):
            preview = "https:" + preview
        info["thumbnail"] = preview

    # views
    m = re.search(r'class="views"[^>]*>[\s\S]*?([\d,]+)\s*views', html, re.I)
    if m:
        info["views"] = m.group(1).replace(",", "")

    return info


def _build_qualities(flashvars: dict) -> List[dict]:
    """ساخت لیست کیفیت‌ها از flashvars."""
    qualities = []

    # video_url (پایه، معمولاً 480p)
    url = flashvars.get("video_url", "")
    text = flashvars.get("video_url_text", "480p")
    if url:
        qualities.append({
            "label": f"📺 {text}",
            "url": url,
            "height": _parse_height(text) or 480,
            "quality_key": "default",
        })

    # video_alt_url (معمولاً 720p)
    url = flashvars.get("video_alt_url", "")
    text = flashvars.get("video_alt_url_text", "720p")
    if url:
        qualities.append({
            "label": f"📺 {text}",
            "url": url,
            "height": _parse_height(text) or 720,
            "quality_key": "alt",
        })

    # video_alt_url2 (معمولاً 1080p)
    url = flashvars.get("video_alt_url2", "")
    text = flashvars.get("video_alt_url2_text", "1080p")
    if url:
        qualities.append({
            "label": f"📺 {text}",
            "url": url,
            "height": _parse_height(text) or 1080,
            "quality_key": "alt2",
        })

    # video_alt_url3, video_alt_url4, ... (بعضی سایت‌ها بیشتر دارن)
    for i in range(3, 10):
        url = flashvars.get(f"video_alt_url{i}", "")
        text = flashvars.get(f"video_alt_url{i}_text", f"{i}")
        if url:
            qualities.append({
                "label": f"📺 {text}",
                "url": url,
                "height": _parse_height(text) or 0,
                "quality_key": f"alt{i}",
            })

    # مرتب‌سازی: بالاترین کیفیت اول
    qualities.sort(key=lambda q: q["height"], reverse=True)

    return qualities


def _parse_height(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d{3,4})", text)
    return int(m.group(1)) if m else None


# ─── Main API ──────────────────────────────────────────────


async def extract_porntrex_qualities(
    url: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict]:
    """
    استخراج کیفیت‌های ویدیو.

    Returns:
        (qualities, title, info)
    """
    if not is_porntrex_url(url):
        return [], "Invalid URL", {}

    if progress_cb:
        await progress_cb("🔄 **در حال دریافت اطلاعات ویدیو...**")

    html = await _fetch_page(url)
    if not html:
        return [], "Failed to fetch page", {}

    flashvars = _parse_flashvars(html)
    if not flashvars:
        logger.warning("flashvars not found for: %s", url)
        return [], "Player data not found", {}

    qualities = _build_qualities(flashvars)
    title = _extract_title(html)
    info = _extract_info(html, flashvars)

    if qualities:
        logger.info(
            "Found %d qualities for: %s", len(qualities), title[:60]
        )

    return qualities, title, info


# ─── Download ───────────────────────────────────────────────


async def download_porntrex_video(
    page_url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو.

    Returns:
        (success, error_message, file_size)
    """
    if not is_porntrex_url(page_url):
        return False, "URL not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0

    headers = {
        **_DEFAULT_HEADERS,
        "Referer": page_url,
        "Accept": "*/*",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)

            async with aiohttp.ClientSession(
                timeout=timeout
            ) as session:
                async with session.get(
                    video_url,
                    headers=headers,
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if 400 <= resp.status < 500:
                            return False, error, 0
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(RETRY_DELAY * attempt)
                            continue
                        return False, error, 0

                    content_length = int(
                        resp.headers.get("Content-Length", 0)
                    )
                    if content_length > MAX_DOWNLOAD_SIZE:
                        return (
                            False,
                            f"Too large: {_format_size(content_length)}",
                            0,
                        )

                    total_mb = (
                        content_length / 1024 / 1024
                        if content_length
                        else 0
                    )
                    downloaded = 0
                    start_time = time.time()
                    last_update = 0.0

                    if progress_cb:
                        size_str = (
                            _format_size(content_length)
                            if content_length
                            else "unknown"
                        )
                        await progress_cb(
                            f"📥 **شروع دانلود...**\n💾 حجم: {size_str}"
                        )

                    async with aiofiles.open(filepath, "wb") as f:
                        async for chunk in resp.content.iter_chunked(
                            CHUNK_SIZE
                        ):
                            await f.write(chunk)
                            downloaded += len(chunk)

                            if downloaded > MAX_DOWNLOAD_SIZE:
                                _cleanup_file(filepath)
                                return False, "Exceeded size limit", 0

                            now = time.time()
                            if (
                                progress_cb
                                and now - last_update >= PROGRESS_INTERVAL
                            ):
                                last_update = now
                                await _report_progress(
                                    progress_cb,
                                    downloaded,
                                    content_length,
                                    total_mb,
                                    start_time,
                                )

            size = os.path.getsize(filepath)
            if size < MIN_FILE_SIZE:
                _cleanup_file(filepath)
                return False, f"Too small ({size} bytes)", 0

            logger.info("Download complete: %s", _format_size(size))
            return True, "", size

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            logger.warning(
                "Download attempt %d/%d: %s", attempt, MAX_RETRIES, e
            )
            _cleanup_file(filepath)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    return False, f"Failed after {MAX_RETRIES} attempts", 0


async def _report_progress(
    progress_cb: ProgressCallback,
    downloaded: int,
    content_length: int,
    total_mb: float,
    start_time: float,
) -> None:
    elapsed = time.time() - start_time
    speed = downloaded / elapsed if elapsed > 0 else 0
    dl_mb = downloaded / 1024 / 1024
    speed_kb = min(speed / 1024, 99999)

    if content_length > 0:
        pct = downloaded / content_length * 100
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        eta_secs = (
            int((content_length - downloaded) / speed) if speed > 0 else 0
        )
        eta_m, eta_s = divmod(eta_secs, 60)

        await progress_cb(
            f"📥 **Downloading...**\n"
            f"`[{bar}]`\n"
            f"💾 {dl_mb:.1f}/{total_mb:.1f} MB  •  "
            f"⚡ {speed_kb:.0f} KB/s\n"
            f"📊 {pct:.1f}%  •  "
            f"⏱ ETA: {eta_m}:{eta_s:02d}"
        )
    else:
        await progress_cb(
            f"📥 **Downloading...**\n"
            f"💾 {dl_mb:.1f} MB  •  ⚡ {speed_kb:.0f} KB/s"
        )


# ─── Wrapper ────────────────────────────────────────────────


async def download_porntrex_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    return await download_porntrex_video(
        url, video_url, filepath, progress_cb
    )
