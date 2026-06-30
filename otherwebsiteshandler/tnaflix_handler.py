"""
tnaflix_handler.py
------------------
استخراج و دانلود از tnaflix.com با نمایش همه کیفیت‌ها.

روش کار:
  1. VIDEO_ID رو از URL استخراج می‌کنیم (الگوی video<digits>)
  2. endpoint /ajax/video-player/<id> یه JSON با فیلد html برمی‌گردونه
  3. داخل html، چند <source> با size (کیفیت) و لینک MP4 مستقیمه
  4. توکن secure زمان‌داره → دانلود مستقیم با aiohttp + progress

نکته: توکن secure زمان‌داره → extract باید بلافاصله قبل از دانلود صدا زده بشه.
"""

import asyncio
import html as html_lib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("TnaflixHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
SESSION_TTL = 30 * 60
MAX_RETRIES = 3
RETRY_DELAY = 2.0

_SITE_DOMAIN = "tnaflix.com"
_SITE_URL = "https://www.tnaflix.com"
_SITE_REFERER = f"{_SITE_URL}/"

_ALLOWED_HOSTS = frozenset({"tnaflix.com", "www.tnaflix.com"})

# دامنه‌های CDN رسانه (sl<NN>.tnaflix.com, cdnl.tnaflix.com, ...)
_ALLOWED_HOST_SUFFIXES = (".tnaflix.com",)

# الگوی استخراج شناسه ویدیو از URL
_VIDEO_ID_RE = re.compile(r"/video(\d+)(?:[/?#]|$)", re.I)

tnaflix_sessions: Dict[str, dict] = {}

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def _is_allowed_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or any(
            host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES
        )
    except Exception:
        return False


def is_tnaflix_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(f".{_SITE_DOMAIN}")
    except Exception:
        return False


def cleanup_expired_sessions() -> int:
    now = time.time()
    expired = [
        sid
        for sid, data in tnaflix_sessions.items()
        if now - data.get("created_at", 0) > SESSION_TTL
    ]
    for sid in expired:
        tnaflix_sessions.pop(sid, None)
    if expired:
        logger.info("Cleaned up %d expired tnaflix sessions", len(expired))
    return len(expired)


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _normalize_url(url: str) -> Optional[str]:
    url = html_lib.unescape(url.replace("\\/", "/").strip())
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return None
    return url


def _extract_video_id(url: str) -> Optional[str]:
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def _quality_sort_key(q: dict) -> int:
    return q.get("height", 0)


def _format_progress(downloaded, content_length, start_time, now) -> str:
    elapsed = now - start_time
    speed = downloaded / elapsed if elapsed > 0 else 0
    dl_mb = downloaded / 1024 / 1024
    if content_length > 0:
        total_mb = content_length / 1024 / 1024
        pct = downloaded / content_length * 100
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        return (
            f"📥 **Downloading...**\n`[{bar}]`\n"
            f"💾 {dl_mb:.1f}/{total_mb:.1f} MB"
            f"  •  ⚡ {speed / 1024 / 1024:.1f} MB/s\n📊 {pct:.1f}%"
        )
    return (
        f"📥 **Downloading...**\n"
        f"💾 {dl_mb:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
    )


# ─── HTTP helpers ───────────────────────────────────────────


@asynccontextmanager
async def _get_session(timeout: Optional[ClientTimeout] = None):
    t = timeout or ClientTimeout(total=30, connect=10)
    session = aiohttp.ClientSession(timeout=t, headers=_DEFAULT_HEADERS)
    try:
        yield session
    finally:
        await session.close()


async def _fetch_with_retry(
    url: str,
    headers: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
    timeout: Optional[ClientTimeout] = None,
) -> Tuple[Optional[str], int]:
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            async with _get_session(timeout) as session:
                merged = {**_DEFAULT_HEADERS, **(headers or {})}
                async with session.get(
                    url, headers=merged, allow_redirects=True
                ) as resp:
                    if resp.status == 200:
                        return await resp.text(errors="replace"), 200
                    last_error = f"HTTP {resp.status}"
                    if 400 <= resp.status < 500:
                        logger.warning("Client error %d for %s", resp.status, url)
                        return None, resp.status
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = str(e)[:120]
            logger.warning(
                "Attempt %d/%d failed for %s: %s",
                attempt,
                max_retries,
                url,
                last_error,
            )
        if attempt < max_retries:
            await asyncio.sleep(RETRY_DELAY * attempt)
    logger.error("All %d attempts failed for %s: %s", max_retries, url, last_error)
    return None, 0


# ─── Extraction ─────────────────────────────────────────────


def _extract_title(html: str) -> str:
    m = re.search(
        r'<meta[^>]+og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
    )
    if m:
        return html_lib.unescape(m.group(1).strip()) or "Untitled"
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*Tnaflix.*$", "", title, flags=re.I).strip()
        return html_lib.unescape(title) or "Untitled"
    return "Untitled"


def _parse_sources(inner_html: str) -> List[dict]:
    """
    <source> tagها رو از HTML پلیر استخراج می‌کنه.
    هر source: src + size (ارتفاع/کیفیت).
    """
    qualities: List[dict] = []
    seen = set()

    for m in re.finditer(r"<source\b[^>]*>", inner_html, re.I):
        tag = m.group(0)
        src_m = re.search(r'src=["\']([^"\']+)["\']', tag, re.I)
        if not src_m:
            continue
        video_url = _normalize_url(src_m.group(1))
        if not video_url or video_url in seen:
            continue
        if not _is_allowed_host(video_url):
            logger.warning("Blocked source host: %s", video_url[:60])
            continue
        seen.add(video_url)

        size_m = re.search(r'size=["\']?(\d+)', tag, re.I)
        if not size_m:
            # fallback: کیفیت از نام فایل (مثل -1080p.mp4)
            size_m = re.search(r"-(\d+)p\.mp4", video_url, re.I)
        height = int(size_m.group(1)) if size_m else 0

        label = f"🎥 {height}p" if height else "🎥 MP4"
        qualities.append(
            {
                "label": label,
                "url": video_url,
                "method": "direct",
                "height": height,
            }
        )

    return qualities


async def extract_tnaflix_qualities(url: str) -> Tuple[List[dict], str]:
    """همه کیفیت‌ها رو از tnaflix استخراج می‌کنه (از endpoint ajax/video-player)."""
    if not is_tnaflix_url(url):
        return [], "Invalid URL"

    cleanup_expired_sessions()

    video_id = _extract_video_id(url)
    if not video_id:
        return [], "Video ID پیدا نشد در URL"

    # عنوان از صفحه اصلی
    page_html, _ = await _fetch_with_retry(url, headers={"Referer": _SITE_REFERER})
    title = _extract_title(page_html) if page_html else "Untitled"

    # endpoint اصلی: ajax/video-player
    ajax_url = f"{_SITE_URL}/ajax/video-player/{video_id}"
    ajax_headers = {
        "Referer": url,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    body, status = await _fetch_with_retry(ajax_url, headers=ajax_headers)

    inner_html = ""
    if body:
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("html"):
                inner_html = html_lib.unescape(data["html"])
        except json.JSONDecodeError:
            inner_html = body

    qualities = _parse_sources(inner_html) if inner_html else []

    # fallback: صفحه player.tnaflix.com
    if not qualities:
        logger.info("ajax endpoint empty, trying player.tnaflix.com")
        player_url = f"https://player.tnaflix.com/video/{video_id}"
        pbody, _ = await _fetch_with_retry(player_url, headers={"Referer": url})
        if pbody:
            qualities = _parse_sources(pbody)

    if not qualities:
        return [], "لینک ویدیو پیدا نشد (ساختار سایت تغییر کرده؟)"

    qualities.sort(key=_quality_sort_key, reverse=True)
    logger.info("Extracted %d qualities for: %s", len(qualities), title[:60])
    return qualities, title


# ─── Download: Direct MP4 ──────────────────────────────────


async def download_tnaflix_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود لینک مستقیم MP4.
    اول multi-segment، بعد aiohttp.
    """
    if not _is_allowed_host(url):
        return False, "URL host not allowed", 0

    # ── روش 1: دانلود چند تیکه‌ای (سریع‌ترین) ──
    logger.info("Download attempt 1: multi-segment (8 connections)")
    success, error, size = await _download_multi_segment(
        url, filepath, progress_cb, num_segments=8
    )
    if success:
        return True, "", size
    logger.info("Multi-segment failed: %s", error)
    _cleanup_file(filepath)

    # ── روش 2: aioHTTP تک connection ──
    headers = {**_DEFAULT_HEADERS, "Referer": _SITE_REFERER}

    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            success, error, size = await _do_direct_download(
                url, filepath, headers, progress_cb
            )
            if success:
                return True, "", size
            if error.startswith("HTTP 4"):
                _cleanup_file(filepath)
                return False, "لینک منقضی شد. لطفاً دوباره لینک ویدیو رو بفرست.", 0
        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            error = str(e)[:150]
            logger.warning(
                "Download attempt %d/%d failed: %s", attempt, MAX_RETRIES, error
            )

        if attempt < MAX_RETRIES:
            _cleanup_file(filepath)
            await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)
    return False, f"Failed after {MAX_RETRIES} attempts: {error}", 0


# ─── Download: Multi-segment (fast) ────────────────────────


async def _download_multi_segment(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    num_segments: int = 8,
) -> Tuple[bool, str, int]:
    """
    دانلود چند تیکه‌ای با چند connection همزمان با aiohttp.
    هر تیکه یه Range request جدا میزنه → سرعت N برابر.
    """
    try:
        timeout = ClientTimeout(total=30, connect=15)
        async with _get_session(timeout) as session:
            async with session.head(
                url, headers={"Referer": _SITE_REFERER}, allow_redirects=True
            ) as resp:
                if resp.status != 200:
                    return False, f"HEAD failed: HTTP {resp.status}", 0

                content_length = int(resp.headers.get("Content-Length", 0))
                accept_ranges = resp.headers.get("Accept-Ranges", "")

                if content_length == 0:
                    return False, "Cannot determine file size", 0

                if content_length > MAX_DOWNLOAD_SIZE:
                    return (
                        False,
                        (f"File too large: {content_length / 1024 / 1024:.0f} MB"),
                        0,
                    )

                if accept_ranges.lower() != "bytes":
                    logger.info("Server doesn't support Range requests, falling back")
                    return False, "Range not supported", 0

        total_mb = content_length / 1024 / 1024
        await progress_cb(
            f"📥 **دانلود چند تیکه‌ای ({num_segments} بخش)...**\n"
            f"💾 حجم: {total_mb:.1f} MB"
        )

        segment_size = content_length // num_segments
        segments = []
        for i in range(num_segments):
            start = i * segment_size
            end = (
                content_length - 1
                if i == num_segments - 1
                else (i + 1) * segment_size - 1
            )
            segments.append((i, start, end))

        segment_files = [f"{filepath}.part{i}" for i in range(num_segments)]
        downloaded_bytes = [0] * num_segments
        start_time = time.time()
        last_update = [0.0]
        lock = asyncio.Lock()

        async def _download_segment(seg_idx: int, byte_start: int, byte_end: int):
            seg_file = segment_files[seg_idx]
            for attempt in range(MAX_RETRIES):
                try:
                    seg_timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
                    async with _get_session(seg_timeout) as session:
                        headers = {
                            "Referer": _SITE_REFERER,
                            "Range": f"bytes={byte_start}-{byte_end}",
                        }
                        async with session.get(
                            url, headers=headers, allow_redirects=True
                        ) as resp:
                            if resp.status not in (200, 206):
                                raise Exception(f"HTTP {resp.status}")

                            async with aiofiles.open(seg_file, "wb") as f:
                                async for chunk in resp.content.iter_chunked(
                                    1024 * 1024
                                ):
                                    if not chunk:
                                        continue
                                    await f.write(chunk)
                                    downloaded_bytes[seg_idx] += len(chunk)

                                    now = time.time()
                                    async with lock:
                                        if now - last_update[0] >= 2.0:
                                            last_update[0] = now
                                            total_dl = sum(downloaded_bytes)
                                            await progress_cb(
                                                _format_progress(
                                                    total_dl,
                                                    content_length,
                                                    start_time,
                                                    now,
                                                )
                                            )
                        return

                except asyncio.CancelledError:
                    _cleanup_file(seg_file)
                    raise
                except Exception as e:
                    logger.warning(
                        "Segment %d attempt %d failed: %s",
                        seg_idx,
                        attempt + 1,
                        e,
                    )
                    _cleanup_file(seg_file)
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2.0 * (attempt + 1))

            raise Exception(f"Segment {seg_idx} failed after {MAX_RETRIES} attempts")

        try:
            await asyncio.gather(
                *[_download_segment(idx, start, end) for idx, start, end in segments]
            )
        except Exception as e:
            for sf in segment_files:
                _cleanup_file(sf)
            return False, str(e)[:200], 0

        await progress_cb("🔗 **ترکیب بخش‌ها...**")
        try:
            async with aiofiles.open(filepath, "wb") as outfile:
                for sf in segment_files:
                    if not os.path.exists(sf):
                        raise FileNotFoundError(f"Missing segment: {sf}")
                    async with aiofiles.open(sf, "rb") as infile:
                        while True:
                            chunk = await infile.read(4 * 1024 * 1024)
                            if not chunk:
                                break
                            await outfile.write(chunk)
        finally:
            for sf in segment_files:
                _cleanup_file(sf)

        if not os.path.exists(filepath):
            return False, "Merged file not created", 0

        final_size = os.path.getsize(filepath)
        if final_size == 0:
            _cleanup_file(filepath)
            return False, "Merged file is empty", 0

        if abs(final_size - content_length) > 1024:
            logger.warning(
                "Size mismatch: expected %d, got %d",
                content_length,
                final_size,
            )

        elapsed = time.time() - start_time
        avg_speed = final_size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        logger.info(
            "Multi-segment download complete: %.1f MB in %.1fs (%.1f MB/s)",
            final_size / 1024 / 1024,
            elapsed,
            avg_speed,
        )

        return True, "", final_size

    except Exception as e:
        logger.warning("Multi-segment download error: %s", e)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


async def _do_direct_download(
    url: str,
    filepath: str,
    headers: dict,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    timeout = ClientTimeout(total=3600, connect=30, sock_read=120)

    async with _get_session(timeout) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}", 0

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > MAX_DOWNLOAD_SIZE:
                size_mb = content_length / 1024 / 1024
                return False, f"File too large: {size_mb:.0f} MB", 0

            downloaded = 0
            start_time = time.time()
            last_update = 0.0

            async with aiofiles.open(filepath, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    await f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_SIZE:
                        _cleanup_file(filepath)
                        return False, "Download exceeded size limit", 0
                    now = time.time()
                    if now - last_update >= 2.0:
                        last_update = now
                        await progress_cb(
                            _format_progress(
                                downloaded, content_length, start_time, now
                            )
                        )

    size = os.path.getsize(filepath)
    if size == 0:
        _cleanup_file(filepath)
        return False, "Downloaded file is empty", 0
    return True, "", size


# ─── سازگاری با API دیگر handlerها ──────────────────────────


async def download_tnaflix_m3u8(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """tnaflix فقط MP4 مستقیم داره؛ این برای سازگاری API هست."""
    return await download_tnaflix_direct(url, filepath, progress_cb)
