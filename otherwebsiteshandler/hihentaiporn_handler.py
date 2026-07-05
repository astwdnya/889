"""
hihentaiporn_handler.py
───────────────────────
استخراج و دانلود ویدیو از hihentaiporn.com

روش کار (بر اساس تحلیل واقعی صفحه):
  - سایت بدون Cloudflare هست (aiohttp ساده کار می‌کنه)
  - Player: KT Player (مثل cartoonporn و leak-sex-tape)
  - URL های ویدیو از /get_file/3/ میان (بدون v-acctoken)
  - ۲ کیفیت اصلی: default (480p) و 720p
  - سرور از Range request پشتیبانی می‌کنه (HTTP 206)
  - yt-dlp نمی‌تونه از صفحه extract کنه ولی روی URL مستقیم کار می‌کنه
  - کوکی‌های PHPSESSID و kt_qparams لازم هستن

استراتژی دانلود:
  1. fetch صفحه با aiohttp (سریع‌ترین)
  2. استخراج URL های ویدیو از HTML (فیلتر preview و screenshots)
  3. Multi-segment download با 16 workers
  4. fallback به single connection
  5. fallback به yt-dlp روی URL مستقیم

وابستگی‌ها:
    pip install aiohttp aiofiles curl_cffi yt-dlp
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse, urljoin, unquote

import aiofiles
import aiohttp
from aiohttp import ClientTimeout, CookieJar

logger = logging.getLogger("HiHentaiPornHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ─── Constants ─────────────────────────────────────────────────────────────

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MIN_VALID_VIDEO_SIZE = 100 * 1024  # 100 KB
PROGRESS_INTERVAL = 1.5
CHUNK_SIZE = 1024 * 1024  # 1 MB
MAX_RETRIES = 3
RETRY_DELAY = 2.0
MULTI_SEGMENT_MIN_SIZE = 5 * 1024 * 1024  # 5 MB
NUM_WORKERS = 16

_ALLOWED_HOSTS = frozenset({
    "hihentaiporn.com",
    "www.hihentaiporn.com",
    "m.hihentaiporn.com",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_hihentaiporn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به hihentaiporn هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".hihentaiporn.com")
    except Exception:
        return False


def _is_main_video_url(url: str) -> bool:
    """بررسی اینکه URL یه ویدیوی اصلی هست (نه preview/screenshot)."""
    url_lower = url.lower()
    # فیلتر preview
    if "preview" in url_lower:
        return False
    if "_preview" in url_lower:
        return False
    # فیلتر screenshots
    if "/contents/videos_screenshots/" in url_lower:
        return False
    # فیلتر /get_file/1/ (این اگه image/gif برمی‌گردونه)
    # فقط /get_file/3/ و /get_file/2/ معتبرن
    if "/get_file/1/" in url_lower:
        return False
    # باید /get_file/ باشه و .mp4
    if "/get_file/" not in url_lower:
        return False
    if ".mp4" not in url_lower:
        return False
    return True


def _cleanup_file(filepath: str) -> None:
    """حذف فایل اگه وجود داشته باشه."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Cleaned up file: %s", filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def _clean_url(url: str) -> str:
    url = unquote(url).replace("&amp;", "&")
    url = re.sub(r'[\\/]+$', '', url)
    return url.strip()


def _format_progress(downloaded, content_length, start_time, now):
    elapsed = now - start_time
    speed = downloaded / elapsed if elapsed > 0 else 0
    dl_mb = downloaded / 1024 / 1024
    if content_length > 0:
        total_mb = content_length / 1024 / 1024
        pct = downloaded / content_length * 100
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        speed_mb = min(speed / 1024 / 1024, 999)
        eta_secs = int((content_length - downloaded) / speed) if speed > 0 else 0
        eta_m, eta_s = divmod(eta_secs, 60)
        return (
            f"📥 **Downloading...**\n`[{bar}]`\n"
            f"💾 {dl_mb:.1f}/{total_mb:.1f} MB  •  ⚡ {speed_mb:.1f} MB/s\n"
            f"📊 {pct:.1f}%  •  ⏱ ETA: {eta_m}:{eta_s:02d}"
        )
    return (
        f"📥 **Downloading...**\n"
        f"💾 {dl_mb:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
    )


def _check_curl_cffi() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


# ─── Extraction ────────────────────────────────────────────────────────────


def _extract_title(html: str) -> str:
    m = re.search(
        r'(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:title["\']',
            html, re.IGNORECASE,
        )
    if m:
        return m.group(1).strip()
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[|]\s*hihentaiporn.*$", "", title, flags=re.IGNORECASE)
        return title or "Untitled"
    return "Untitled"


def _extract_thumbnail(html: str) -> str:
    m = re.search(
        r'(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return ""


def _extract_duration(html: str) -> Optional[int]:
    """استخراج مدت ویدیو از JSON-LD (PT0H15M56S format)."""
    m = re.search(r'"duration"\s*:\s*"(PT[^"]+)"', html)
    if m:
        duration_str = m.group(1)
        # PT0H15M56S → 956 seconds
        h = re.search(r'(\d+)H', duration_str)
        m_min = re.search(r'(\d+)M', duration_str)
        s = re.search(r'(\d+)S', duration_str)
        total = 0
        if h: total += int(h.group(1)) * 3600
        if m_min: total += int(m_min.group(1)) * 60
        if s: total += int(s.group(1))
        return total if total > 0 else None
    return None


def _extract_video_sources(html: str) -> List[dict]:
    """
    استخراج URL های ویدیو اصلی از HTML.

    فیلتر می‌کنه:
      - preview.mp4 (ویدیوهای مرتبط)
      - screenshots
      - /get_file/1/ (ad redirect)
    """
    sources = []
    seen_urls = set()

    # الگوی URL های /get_file/ با .mp4
    # فرمت: https://hihentaiporn.com/get_file/3/HASH/2000/2191/2191.mp4
    # یا:   https://hihentaiporn.com/get_file/3/HASH/2000/2191/2191_720p.mp4?br=2348
    pattern = re.compile(
        r'(https?://[^\s"\'<>\)\]]+?/get_file/[23]/[^\s"\'<>\)\]]+?\.mp4(?:\?[^\s"\'<>\)\]]*)?)',
        re.IGNORECASE,
    )

    for m in pattern.finditer(html):
        url = _clean_url(m.group(1))
        if not _is_main_video_url(url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # تشخیص کیفیت از URL
        url_lower = url.lower()
        if "_720p" in url_lower:
            label = "📺 MP4 720p"
            height = 720
            quality_key = "720p"
        elif "_1080p" in url_lower:
            label = "📺 MP4 1080p"
            height = 1080
            quality_key = "1080p"
        elif "_480p" in url_lower:
            label = "📺 MP4 480p"
            height = 480
            quality_key = "480p"
        elif "_360p" in url_lower:
            label = "📺 MP4 360p"
            height = 360
            quality_key = "360p"
        else:
            # default (بدون پسوند کیفیت) - معمولاً 480p
            label = "📺 MP4 (default)"
            height = 480
            quality_key = "default"

        sources.append({
            "label": label,
            "url": url,
            "height": height,
            "quality_key": quality_key,
            "method": "get_file",
        })

    # مرتب‌سازی: بالاترین کیفیت اول
    sources.sort(key=lambda q: q.get("height", 0), reverse=True)
    return sources


async def _fetch_page(
    url: str,
    jar: Optional[CookieJar] = None,
) -> Tuple[Optional[str], Optional[CookieJar], str]:
    """
    fetch صفحه. اول aiohttp، بعد curl_cffi.

    Returns:
        (html, cookie_jar, error_message)
    """
    # ── روش 1: aiohttp (سایت بدون Cloudflare، کار می‌کنه) ──
    try:
        local_jar = jar or CookieJar(unsafe=True)
        timeout = ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(
            timeout=timeout, headers=_DEFAULT_HEADERS, cookie_jar=local_jar
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="replace")
                    logger.info("Page fetched via aiohttp, size=%d", len(html))
                    return html, local_jar, ""
                logger.warning("aiohttp fetch: HTTP %s", resp.status)
    except Exception as e:
        logger.warning(f"aiohttp fetch error: {e}")

    # ── روش 2: curl_cffi (fallback) ──
    if _check_curl_cffi():
        try:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession() as session:
                resp = await session.get(
                    url,
                    impersonate="chrome",
                    headers={"Accept-Language": "en-US,en;q=0.9"},
                    allow_redirects=True,
                    timeout=30,
                )
                if resp.status_code == 200 and resp.text:
                    logger.info("Page fetched via curl_cffi, size=%d", len(resp.text))
                    return resp.text, None, ""
                logger.warning("curl_cffi fetch: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning(f"curl_cffi fetch error: {e}")

    return None, jar, "Failed to fetch page"


async def extract_hihentaiporn_qualities(
    url: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict]:
    """
    استخراج کیفیت‌های ویدیو.

    Returns:
        (qualities, title, info)
    """
    if not is_hihentaiporn_url(url):
        return [], "Invalid URL", {}

    if progress_cb:
        await progress_cb("🔄 **دریافت اطلاعات صفحه...**")

    jar = CookieJar(unsafe=True)
    html, jar, error = await _fetch_page(url, jar)

    if not html:
        logger.error("Failed to fetch page: %s", error)
        return [], f"خطا در دریافت صفحه: {error}", {}

    title = _extract_title(html)
    thumbnail = _extract_thumbnail(html)
    duration = _extract_duration(html)
    sources = _extract_video_sources(html)

    if not sources:
        logger.error("No video sources found")
        return [], "URL ویدیو در صفحه پیدا نشد", {}

    # استخراج کوکی‌ها
    cookies = {}
    if jar:
        for cookie in jar:
            cookies[cookie.key] = cookie.value

    logger.info("Found %d video sources, cookies: %s", len(sources), list(cookies.keys()))

    if progress_cb:
        labels = ", ".join(s["label"] for s in sources)
        dur_str = ""
        if duration:
            mins, secs = divmod(duration, 60)
            dur_str = f" ({mins}:{secs:02d})"
        await progress_cb(f"✅ **پیدا شد:** {title[:50]}{dur_str}\n🎞 کیفیت‌ها: {labels}")

    return sources, title, {
        "thumbnail": thumbnail,
        "page_url": url,
        "cookies": cookies,
        "duration": duration,
        "fetch_method": "aiohttp",
    }


# ─── Download: Multi-segment (work-queue) ─────────────────────────────────


# متغیر module-level برای cancel support (با bot.py سازگار)
active_downloads: dict = {}


async def _download_multi_segment(
    direct_url: str,
    filepath: str,
    referer: str,
    cookies: dict,
    progress_cb: ProgressCallback,
    dl_id: str = "",
    num_workers: int = NUM_WORKERS,
) -> Tuple[bool, str, int]:
    """
    دانلود چند تیکه‌ای با work-queue pattern.
    از aiohttp استفاده می‌کنه چون سایت بدون Cloudflare هست.
    """
    try:
        # ── HEAD request برای گرفتن حجم ──
        headers = {**_DEFAULT_HEADERS, "Referer": referer, "Accept": "*/*"}
        timeout = ClientTimeout(total=10, connect=5)
        content_length = 0
        accept_ranges = ""

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookies=cookies) as s:
                async with s.head(direct_url, allow_redirects=True) as r:
                    if r.status in (200, 206):
                        content_length = int(r.headers.get("Content-Length", 0))
                        accept_ranges = r.headers.get("Accept-Ranges", "").lower()
                    elif r.status == 403:
                        return False, "HTTP_403", 0
        except Exception as e:
            logger.warning(f"HEAD request failed: {e}")

        # اگه HEAD کار نکرد، probe با Range
        if content_length == 0:
            try:
                timeout = ClientTimeout(total=10, connect=5)
                async with aiohttp.ClientSession(timeout=timeout, headers={**headers, "Range": "bytes=0-0"}) as s:
                    async with s.get(direct_url, allow_redirects=True) as r:
                        if r.status in (200, 206):
                            content_length = int(r.headers.get("Content-Length", 0))
                            if r.status == 206:
                                accept_ranges = "bytes"
                                cr = r.headers.get("Content-Range", "")
                                m = re.search(r"/(\d+)", cr)
                                if m:
                                    content_length = int(m.group(1))
            except Exception as e:
                logger.warning(f"Probe request failed: {e}")

        if content_length == 0:
            return False, "Cannot determine file size", 0
        if content_length > MAX_DOWNLOAD_SIZE:
            return False, f"File too large: {_format_size(content_length)}", 0
        if accept_ranges != "bytes" or content_length < MULTI_SEGMENT_MIN_SIZE:
            return False, "Range not supported or file too small", 0

        total_mb = content_length / 1024 / 1024
        await progress_cb(
            f"📥 **Downloading...**\n💾 Size: {total_mb:.1f} MB"
        )

        # ── Work-queue pattern ──
        CHUNK_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
        chunks = []
        offset = 0
        chunk_idx = 0
        while offset < content_length:
            end = min(offset + CHUNK_SIZE_BYTES - 1, content_length - 1)
            chunks.append((chunk_idx, offset, end))
            offset = end + 1
            chunk_idx += 1

        total_chunks = len(chunks)
        logger.info(f"[DL-HIHP] Work-queue: {total_chunks} chunks, {num_workers} workers, total={content_length}")

        # ساخت فایل sparse
        try:
            with open(filepath, "wb") as f:
                f.truncate(content_length)
        except Exception as e:
            logger.warning(f"Could not pre-allocate: {e}")

        chunk_queue = asyncio.Queue()
        for c in chunks:
            await chunk_queue.put(c)

        downloaded_bytes = [0] * total_chunks
        completed_chunks = [0]
        failed_chunks = []
        start_time = time.time()
        last_update = [0.0]
        progress_lock = asyncio.Lock()
        file_write_lock = asyncio.Lock()
        first_chunk_started = [False]

        async def _update_progress(force: bool = False):
            now = time.time()
            if not force and now - last_update[0] < PROGRESS_INTERVAL:
                return
            last_update[0] = now
            total_dl = sum(downloaded_bytes)
            elapsed = now - start_time
            speed = total_dl / elapsed if elapsed > 0 else 0
            dl_mb = total_dl / 1024 / 1024
            total_mb_local = content_length / 1024 / 1024
            pct = (total_dl / content_length * 100) if content_length > 0 else 0
            filled = int(pct / 5)
            bar = "█" * filled + "░" * (20 - filled)
            speed_mb = min(speed / 1024 / 1024, 999)
            eta_secs = int((content_length - total_dl) / speed) if speed > 0 else 0
            eta_m, eta_s = divmod(eta_secs, 60)
            try:
                await progress_cb(
                    f"📥 **Downloading...**\n`[{bar}]`\n"
                    f"💾 {dl_mb:.1f}/{total_mb_local:.1f} MB  •  ⚡ {speed_mb:.1f} MB/s\n"
                    f"📊 {pct:.1f}%  •  ⏱ ETA: {eta_m}:{eta_s:02d}\n"
                    f"📦 {completed_chunks[0]}/{total_chunks} chunks • 🔥 {num_workers}x"
                )
            except Exception:
                pass

        # session اشتراکی برای همه worker ها
        shared_timeout = ClientTimeout(total=300, connect=30, sock_read=120)
        shared_session = aiohttp.ClientSession(
            timeout=shared_timeout, headers=headers, cookies=cookies
        )

        async def _download_worker(worker_id: int):
            while True:
                if active_downloads.get(dl_id, {}).get("cancelled"):
                    return False
                try:
                    chunk_info = chunk_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return True

                c_idx, byte_start, byte_end = chunk_info
                chunk_size = byte_end - byte_start + 1
                max_retries = 3

                for attempt in range(max_retries):
                    if active_downloads.get(dl_id, {}).get("cancelled"):
                        return False
                    try:
                        async with shared_session.get(
                            direct_url,
                            headers={"Range": f"bytes={byte_start}-{byte_end}"},
                            allow_redirects=True,
                        ) as resp:
                            if resp.status not in (200, 206):
                                raise Exception(f"HTTP {resp.status}")

                            if not first_chunk_started[0]:
                                first_chunk_started[0] = True
                                await _update_progress(force=True)

                            chunk_data = b""
                            async for piece in resp.content.iter_chunked(CHUNK_SIZE):
                                if not piece:
                                    continue
                                if active_downloads.get(dl_id, {}).get("cancelled"):
                                    return False
                                chunk_data += piece

                            if len(chunk_data) != chunk_size:
                                raise Exception(f"Size mismatch: {len(chunk_data)} vs {chunk_size}")

                            async with file_write_lock:
                                async with aiofiles.open(filepath, "r+b") as f:
                                    await f.seek(byte_start)
                                    await f.write(chunk_data)

                            downloaded_bytes[c_idx] = chunk_size
                            async with progress_lock:
                                completed_chunks[0] += 1
                                await _update_progress()
                            break

                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[DL-HIHP] W{worker_id} c{c_idx} attempt {attempt+1} failed: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 * (attempt + 1))
                        else:
                            failed_chunks.append((c_idx, str(e)[:100]))
                            return False

                chunk_queue.task_done()
            return True

        try:
            results = await asyncio.gather(
                *[_download_worker(i) for i in range(num_workers)],
                return_exceptions=True,
            )
            try:
                await shared_session.close()
            except Exception:
                pass

            if active_downloads.get(dl_id, {}).get("cancelled"):
                _cleanup_file(filepath)
                return False, "Cancelled by user", 0

            worker_failures = [r for r in results if r is not True and isinstance(r, bool) and not r]
            if worker_failures or failed_chunks:
                logger.warning(f"[DL-HIHP] {len(worker_failures)} workers failed, {len(failed_chunks)} chunks failed")
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-HIHP] Work-queue error: {e}", exc_info=True)
            try:
                await shared_session.close()
            except Exception:
                pass
            _cleanup_file(filepath)
            return False, str(e)[:200], 0

        file_size = os.path.getsize(filepath)
        if file_size < MIN_VALID_VIDEO_SIZE:
            _cleanup_file(filepath)
            return False, f"File too small ({file_size} bytes)", 0

        if file_size != content_length:
            logger.warning(f"[DL-HIHP] Size mismatch: expected={content_length}, got={file_size}")

        elapsed = time.time() - start_time
        avg_speed = file_size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        logger.info(f"[DL-HIHP] DONE | size={_format_size(file_size)} | time={elapsed:.1f}s | avg={avg_speed:.1f} MB/s")
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-HIHP] Multi-segment error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Download: Single connection (fallback) ───────────────────────────────


async def _download_single_aiohttp(
    url: str,
    filepath: str,
    referer: str,
    cookies: dict,
    progress_cb: ProgressCallback,
    dl_id: str = "",
) -> Tuple[bool, str, int]:
    """دانلود با single connection (fallback)."""
    headers = {**_DEFAULT_HEADERS, "Referer": referer}
    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookies=cookies) as s:
                async with s.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if 400 <= resp.status < 500:
                            _cleanup_file(filepath)
                            return False, error, 0
                    else:
                        content_length = int(resp.headers.get("Content-Length", 0))
                        if content_length > MAX_DOWNLOAD_SIZE:
                            return False, f"File too large: {_format_size(content_length)}", 0

                        downloaded = 0
                        start_time = time.time()
                        last_update = 0.0

                        async with aiofiles.open(filepath, "wb") as f:
                            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                                if active_downloads.get(dl_id, {}).get("cancelled"):
                                    _cleanup_file(filepath)
                                    return False, "Cancelled by user", 0
                                await f.write(chunk)
                                downloaded += len(chunk)

                                now = time.time()
                                if now - last_update >= PROGRESS_INTERVAL:
                                    last_update = now
                                    await progress_cb(
                                        _format_progress(downloaded, content_length, start_time, now)
                                    )

                        size = os.path.getsize(filepath)
                        if size < MIN_VALID_VIDEO_SIZE:
                            _cleanup_file(filepath)
                            return False, f"File too small ({size} bytes)", 0
                        return True, "", size

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            error = str(e)[:200]
        if attempt < MAX_RETRIES:
            _cleanup_file(filepath)
            await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)
    return False, f"Failed after {MAX_RETRIES} attempts: {error}", 0


# ─── Download: yt-dlp (fallback) ──────────────────────────────────────────


async def _download_with_ytdlp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود با yt-dlp روی URL مستقیم."""
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    await progress_cb("📥 **Fallback: yt-dlp...**")

    try:
        cmd = [
            "yt-dlp", "--no-warnings", "--progress", "--newline",
            "--no-check-certificates",
            "-f", "best",
            "--concurrent-fragments", "16",
            "--retries", "10", "--fragment-retries", "10",
            "--buffer-size", "16K",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "-o", filepath,
            url,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )

        last_update = 0.0
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=300)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                _cleanup_file(filepath)
                return False, "Download timed out", 0
            if not line:
                break
            text = line.decode(errors="replace").strip()
            now = time.time()
            if now - last_update >= PROGRESS_INTERVAL and text:
                last_update = now
                if "[download]" in text:
                    msg = _parse_ytdlp_progress(text)
                    if msg:
                        await progress_cb(msg)

        await process.wait()
        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            err_msg = _extract_ytdlp_error(stderr)
            return False, err_msg[:200], 0

        actual_path = _find_output_file(filepath)
        if not actual_path:
            return False, "Output file not found", 0

        size = os.path.getsize(actual_path)
        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(actual_path)
            return False, "File exceeds size limit", 0
        if size < MIN_VALID_VIDEO_SIZE:
            _cleanup_file(actual_path)
            return False, f"File too small ({size} bytes)", 0

        if actual_path != filepath:
            try:
                os.rename(actual_path, filepath)
            except OSError:
                pass

        logger.info(f"[DL-HIHP] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-HIHP] yt-dlp error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


def _parse_ytdlp_progress(text: str) -> Optional[str]:
    pct_match = re.search(r"(\d+\.?\d*)%", text)
    if not pct_match:
        return None
    pct = pct_match.group(1)
    size_match = re.search(r"of\s+~?\s*([\d.]+\s*\w+)", text)
    speed_match = re.search(r"at\s+([\d.]+\s*\w+/s)", text)
    eta_match = re.search(r"ETA\s+(\S+)", text)
    total = size_match.group(1) if size_match else "?"
    speed = speed_match.group(1) if speed_match else "?"
    eta = eta_match.group(1) if eta_match else "?"
    try:
        pct_num = float(pct)
        filled = int(pct_num / 5)
        bar = "█" * filled + "░" * (20 - filled)
    except (ValueError, TypeError):
        bar = "░" * 20
    return (
        f"📥 **Downloading...**\n`[{bar}]`\n"
        f"💾 {total}  •  ⚡ {speed}\n📊 {pct}%  •  ⏱ ETA: {eta}"
    )


def _extract_ytdlp_error(stderr: str) -> str:
    if not stderr:
        return "Unknown error"
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("ERROR:"):
            return line[6:].strip()[:200]
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    return lines[-1][:200] if lines else "Unknown error"


def _find_output_file(filepath: str) -> Optional[str]:
    if os.path.exists(filepath):
        return filepath
    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = base + ext
        if os.path.exists(candidate):
            return candidate
    return None


# ─── Public API ────────────────────────────────────────────────────────────


async def download_hihentaiporn_video(
    page_url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    cookies: Optional[dict] = None,
    dl_id: str = "",
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو از hihentaiporn.

    استراتژی:
      1. multi-segment download با aiohttp (16 workers)
      2. single-connection با aiohttp (fallback)
      3. yt-dlp روی URL مستقیم (fallback نهایی)

    Args:
        page_url: URL صفحه ویدیو (برای Referer)
        video_url: URL مستقیم ویدیو
        filepath: مسیر ذخیره فایل
        progress_cb: callback پیشرفت
        cookies: کوکی‌های session
        dl_id: download ID برای cancel
    """
    if not is_hihentaiporn_url(page_url):
        return False, "URL host not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0

    if progress_cb is None:
        async def _noop(msg): pass
        progress_cb = _noop

    if dl_id and dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}

    referer = page_url
    if not cookies:
        cookies = {}

    # ── روش 1: multi-segment ──
    logger.info("[DL-HIHP] Attempt 1: multi-segment")
    success, error, size = await _download_multi_segment(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0
    logger.info(f"[DL-HIHP] Multi-segment failed: {error}")
    _cleanup_file(filepath)

    # ── روش 2: single-connection ──
    logger.info("[DL-HIHP] Attempt 2: single-connection")
    success, error, size = await _download_single_aiohttp(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    logger.info(f"[DL-HIHP] Single failed: {error}")
    _cleanup_file(filepath)

    # ── روش 3: yt-dlp ──
    logger.info("[DL-HIHP] Attempt 3: yt-dlp")
    success, error, size = await _download_with_ytdlp(
        video_url, filepath, progress_cb,
    )
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


async def download_hihentaiporn_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
    quality: str = "high",
    dl_id: str = "",
) -> Tuple[bool, str, int]:
    """
    Wrapper برای سازگاری با bot architecture.

    Args:
        url: URL صفحه ویدیو
        filepath: مسیر ذخیره
        progress_cb: callback پیشرفت
        video_url: (اختیاری) URL مستقیم ویدیو
        quality: 'high', 'low', '720p', '480p', '1080p'
        dl_id: download ID برای cancel
    """
    if not video_url:
        qualities, title, info = await extract_hihentaiporn_qualities(
            url, progress_cb
        )
        if not qualities:
            return False, title or "Extraction failed", 0

        # انتخاب کیفیت
        selected = None
        for q in qualities:
            if q.get("quality_key") == quality:
                selected = q
                break
        if not selected:
            if quality in ("high", "best"):
                selected = qualities[0]
            elif quality in ("low", "worst"):
                selected = qualities[-1]
            else:
                selected = qualities[0]

        video_url = selected["url"]
        cookies = info.get("cookies", {})
    else:
        # اگه video_url داده شده، باید cookies رو هم fetch کنیم
        qualities, title, info = await extract_hihentaiporn_qualities(
            url, progress_cb
        )
        cookies = info.get("cookies", {}) if info else {}

    return await download_hihentaiporn_video(
        url, video_url, filepath, progress_cb,
        cookies=cookies, dl_id=dl_id,
    )
