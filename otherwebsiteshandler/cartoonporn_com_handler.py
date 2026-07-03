"""
cartoonporn_handler.py
──────────────────────
استخراج و دانلود ویدیو از cartoonporn.com

روش کار (بر اساس تحلیل واقعی صفحه):
  - سایت پشت Cloudflare هست (aiohttp ساده 403 می‌گیره)
  - curl_cffi با impersonate=chrome کار می‌کنه
  - Player: KT Player (مثل leak-sex-tape)
  - URL های ویدیو از /get_file/ میان با v-acctoken token
  - کوکی kt_acctoken برای دسترسی به CDN لازمه
  - PHPSESSID برای session persistence لازمه
  - ?asgtbndr=1 در URL اصلی مهمه (kt_rt_asgtbndr cookie)
  - ۲ کیفیت: 720p و 480p (default)
  - URL امضا‌دار با TTL کوتاه (باید هر بار fetch کنی)

استراتژی دانلود:
  1. fetch صفحه با curl_cffi (impersonate=chrome) + حفظ session
  2. استخراج URL های ویدیو از flashvars / HTML
  3. دانلود با curl_cffi (همون session که fetch کردیم)
  4. fallback به yt-dlp با --impersonate=chrome روی URL صفحه

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
from urllib.parse import urlparse, urljoin, unquote, quote

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("CartoonPornHandler")

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
MULTI_SEGMENT_WORKERS = 16  # مثل بقیه هندلرها

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset({
    "cartoonporn.com",
    "www.cartoonporn.com",
    "m.cartoonporn.com",
})

# CDN مجاز (فقط برای screenshot/preview — ویدیو اصلی از cartoonporn.com/get_file/)
_ALLOWED_CDN_HOSTS = frozenset({
    "mjedge.net",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_cartoonporn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به cartoonporn هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".cartoonporn.com")
    except Exception:
        return False


def _is_main_video_url(url: str) -> bool:
    """بررسی اینکه URL یه ویدیوی اصلی هست (نه preview)."""
    if "preview" in url.lower():
        return False
    if "_preview" in url.lower():
        return False
    if "screenshot" in url.lower():
        return False
    return "/get_file/" in url and ".mp4" in url.lower()


def _cleanup_file(filepath: str) -> None:
    """حذف فایل اگه وجود داشته باشه."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Cleaned up file: %s", filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _format_size(size_bytes: int) -> str:
    """فرمت حجم فایل."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def _clean_url(url: str) -> str:
    """نظافت URL."""
    url = unquote(url).replace("&amp;", "&")
    url = re.sub(r'[\\/]+$', '', url)
    return url.strip()


def _format_progress(
    downloaded: int,
    content_length: int,
    start_time: float,
    now: float,
) -> str:
    """فرمت پیام پیشرفت دانلود."""
    elapsed = now - start_time
    speed = downloaded / elapsed if elapsed > 0 else 0
    dl_mb = downloaded / 1024 / 1024

    if content_length > 0:
        total_mb = content_length / 1024 / 1024
        pct = downloaded / content_length * 100
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        speed_mb = min(speed / 1024 / 1024, 999)
        eta_secs = (
            int((content_length - downloaded) / speed) if speed > 0 else 0
        )
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
    """بررسی نصب بودن curl_cffi."""
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


# ─── Extraction ────────────────────────────────────────────────────────────


def _extract_title(html: str) -> str:
    """استخراج عنوان ویدیو."""
    # روش 1: og:title
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
        title = m.group(1).strip()
        # حذف " - CartoonPorn.com"
        title = re.sub(
            r"\s*[-|]\s*CartoonPorn\.com\s*$",
            "", title, flags=re.IGNORECASE,
        )
        return title

    # روش 2: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(
            r"\s*[-|]\s*CartoonPorn\.com\s*$",
            "", title, flags=re.IGNORECASE,
        )
        return title or "Untitled"

    return "Untitled"


def _extract_thumbnail(html: str) -> str:
    """استخراج thumbnail از og:image."""
    m = re.search(
        r'(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return ""


def _extract_video_sources(html: str) -> List[dict]:
    """
    استخراج URL های ویدیو اصلی از HTML.

    اولویت 1: URL های با v-acctoken (از flashvars یا HTML)
    اولویت 2: URL های /get_file/ با پسوند mp4

    Returns:
        list of dicts: [{label, url, height, quality_key}, ...]
    """
    sources = []
    seen_urls = set()

    # روش 1: پیدا کردن URL های با v-acctoken (این بهترین نوعه)
    # فرمت: https://www.cartoonporn.com/get_file/1/HASH/29000/29509/29509_720p.mp4/?v-acctoken=TOKEN
    vacctoken_pattern = re.compile(
        r'(https?://[^\s"\'<>\)\]]+?/get_file/[^\s"\'<>\)\]]+?\.mp4[^\s"\'<>\)\]]*?\?v-acctoken=[a-zA-Z0-9+/=_-]+)',
        re.IGNORECASE,
    )
    for m in vacctoken_pattern.finditer(html):
        url = _clean_url(m.group(1))
        if not _is_main_video_url(url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # تشخیص کیفیت از URL
        if "_720p" in url.lower():
            label = "📺 MP4 720p"
            height = 720
            quality_key = "720p"
        elif "_480p" in url.lower():
            label = "📺 MP4 480p"
            height = 480
            quality_key = "480p"
        elif "_1080p" in url.lower():
            label = "📺 MP4 1080p"
            height = 1080
            quality_key = "1080p"
        elif "_360p" in url.lower():
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
            "method": "v-acctoken",
        })

    # روش 2: پیدا کردن URL های /get_file/ بدون v-acctoken (که بعداً باید token اضافه کنیم)
    getfile_pattern = re.compile(
        r'(https?://[^\s"\'<>\)\]]+?/get_file/[^\s"\'<>\)\]]+?\.mp4)(?:[/?\s"\'<>\)\]]|$)',
        re.IGNORECASE,
    )
    for m in getfile_pattern.finditer(html):
        url = _clean_url(m.group(1))
        if not _is_main_video_url(url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # تشخیص کیفیت
        if "_720p" in url.lower():
            label = "📺 MP4 720p"
            height = 720
            quality_key = "720p"
        elif "_480p" in url.lower():
            label = "📺 MP4 480p"
            height = 480
            quality_key = "480p"
        elif "_1080p" in url.lower():
            label = "📺 MP4 1080p"
            height = 1080
            quality_key = "1080p"
        elif "_360p" in url.lower():
            label = "📺 MP4 360p"
            height = 360
            quality_key = "360p"
        else:
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


async def _fetch_page_curl_cffi(
    url: str,
    cookies: Optional[dict] = None,
) -> Tuple[Optional[str], Optional[dict], Optional[dict], str]:
    """
    fetch صفحه با curl_cffi (با حفظ session).

    Returns:
        (html, response_headers, cookies_dict, error_message)
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None, None, None, "curl_cffi not installed"

    try:
        async with AsyncSession() as session:
            # اگه کوکی داریم، set کن
            if cookies:
                for name, value in cookies.items():
                    try:
                        session.cookies.set(name, value)
                    except Exception:
                        pass

            resp = await session.get(
                url,
                impersonate="chrome",
                headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
                allow_redirects=True,
                timeout=30,
            )

            if resp.status_code == 200 and resp.text:
                logger.info("Page fetched via curl_cffi (chrome), size=%d", len(resp.text))
                # استخراج کوکی‌ها از response
                cookies_dict = {}
                try:
                    for cookie in session.cookies.jar:
                        cookies_dict[cookie.name] = cookie.value
                except Exception:
                    # روش جایگزین
                    for cookie in session.cookies:
                        try:
                            if hasattr(cookie, 'name') and hasattr(cookie, 'value'):
                                cookies_dict[cookie.name] = cookie.value
                        except Exception:
                            pass

                return resp.text, dict(resp.headers), cookies_dict, ""

            return None, dict(resp.headers) if resp.headers else None, None, f"HTTP {resp.status_code}"

    except Exception as e:
        logger.error(f"curl_cffi fetch error: {e}")
        return None, None, None, str(e)[:200]


async def extract_cartoonporn_qualities(
    url: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict]:
    """
    استخراج کیفیت‌های ویدیو.

    Returns:
        (qualities, title, info)
        qualities: list of dicts with keys: label, url, height, quality_key, method
        title: str
        info: dict with extra info (thumbnail, cookies, etc.)
    """
    if not is_cartoonporn_url(url):
        return [], "Invalid URL", {}

    if progress_cb:
        await progress_cb("🔄 **دریافت اطلاعات صفحه...**")

    # fetch صفحه با curl_cffi
    html, resp_headers, cookies_dict, error = await _fetch_page_curl_cffi(url)

    if not html:
        logger.error("Failed to fetch page: %s", error)
        return [], f"خطا در دریافت صفحه: {error}", {}

    # استخراج اطلاعات
    title = _extract_title(html)
    thumbnail = _extract_thumbnail(html)
    sources = _extract_video_sources(html)

    if not sources:
        logger.error("No video sources found in page")
        return [], "URL ویدیو در صفحه پیدا نشد", {}

    logger.info(
        "Found %d video sources (cookies: %s)",
        len(sources), list(cookies_dict.keys()) if cookies_dict else "none",
    )

    if progress_cb:
        labels = ", ".join(s["label"] for s in sources)
        await progress_cb(f"✅ **پیدا شد:** {title[:50]}\n🎞 کیفیت‌ها: {labels}")

    return sources, title, {
        "thumbnail": thumbnail,
        "page_url": url,
        "cookies": cookies_dict or {},
        "fetch_method": "curl_cffi/chrome",
    }


# ─── Download: Multi-segment (fast) ────────────────────────────────────────


async def _download_multi_segment(
    direct_url: str,
    filepath: str,
    referer: str,
    cookies: dict,
    progress_cb: ProgressCallback,
    num_workers: int = MULTI_SEGMENT_WORKERS,
) -> Tuple[bool, str, int]:
    """
    دانلود چند تیکه‌ای با work-queue pattern.
    از session اشتراکی استفاده می‌کنه برای جلوگیری از TLS handshake مکرر.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        # ── HEAD request با session اشتراکی ──
        shared_session = AsyncSession()

        # set cookies
        for name, value in cookies.items():
            try:
                shared_session.cookies.set(name, value)
            except Exception:
                pass

        # HEAD request با timeout کوتاه
        try:
            head_resp = await shared_session.head(
                direct_url,
                impersonate="chrome",
                headers={
                    "Referer": referer,
                    "Accept": "*/*",
                },
                allow_redirects=True,
                timeout=10,
            )

            content_length = int(head_resp.headers.get("Content-Length", 0))
            accept_ranges = head_resp.headers.get("Accept-Ranges", "").lower()

            if head_resp.status_code == 403:
                try:
                    await shared_session.close()
                except Exception:
                    pass
                return False, "HTTP_403", 0

            if head_resp.status_code not in (200, 206):
                logger.warning("HEAD returned HTTP %s, will try GET", head_resp.status_code)
                content_length = 0
                accept_ranges = ""
        except Exception as e:
            logger.warning(f"HEAD request failed: {e}, will try GET")
            content_length = 0
            accept_ranges = ""

        # اگه HEAD کار نکرد، یه GET کوچیک بزن تا حجم رو بفهم
        if content_length == 0:
            try:
                probe_resp = await shared_session.get(
                    direct_url,
                    impersonate="chrome",
                    headers={
                        "Referer": referer,
                        "Accept": "*/*",
                        "Range": "bytes=0-0",
                    },
                    allow_redirects=True,
                    timeout=10,
                )
                if probe_resp.status_code in (200, 206):
                    content_length = int(probe_resp.headers.get("Content-Length", 0))
                    accept_ranges = probe_resp.headers.get("Accept-Ranges", "").lower()
                    # اگه 206 گرفتیم، یعنی Range پشتیبانی می‌شه
                    if probe_resp.status_code == 206:
                        accept_ranges = "bytes"
                        # استخراج حجم کل از Content-Range
                        cr = probe_resp.headers.get("Content-Range", "")
                        m = re.search(r"/(\d+)", cr)
                        if m:
                            content_length = int(m.group(1))
            except Exception as e:
                logger.warning(f"Probe request failed: {e}")

        if content_length == 0:
            try:
                await shared_session.close()
            except Exception:
                pass
            return False, "Cannot determine file size", 0

        if content_length > MAX_DOWNLOAD_SIZE:
            try:
                await shared_session.close()
            except Exception:
                pass
            return False, f"File too large: {_format_size(content_length)}", 0

        # اگه Range پشتیبانی نمی‌شه یا فایل کوچیکه، single connection
        if accept_ranges != "bytes" or content_length < MULTI_SEGMENT_MIN_SIZE:
            try:
                await shared_session.close()
            except Exception:
                pass
            return False, "Range not supported or file too small", 0

        total_mb = content_length / 1024 / 1024
        await progress_cb(
            f"📥 **Downloading...**\n"
            f"💾 Size: {total_mb:.1f} MB"
        )

        # ── Work-queue pattern ──
        CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB per chunk
        chunks = []
        offset = 0
        chunk_idx = 0
        while offset < content_length:
            end = min(offset + CHUNK_SIZE - 1, content_length - 1)
            chunks.append((chunk_idx, offset, end))
            offset = end + 1
            chunk_idx += 1

        total_chunks = len(chunks)
        logger.info(f"[DL-CARTOON] Work-queue: {total_chunks} chunks, {num_workers} workers, total={content_length}")

        # ساخت فایل sparse
        try:
            with open(filepath, "wb") as f:
                f.truncate(content_length)
        except Exception as e:
            logger.warning(f"Could not pre-allocate file: {e}")

        # Queue و متغیرهای مشترک
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

        async def _download_worker(worker_id: int):
            """هر worker از queue chunk می‌گیره و دانلود می‌کنه."""
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
                        resp = await shared_session.get(
                            direct_url,
                            impersonate="chrome",
                            headers={
                                "Accept": "*/*",
                                "Accept-Language": "en-US,en;q=0.9",
                                "Referer": referer,
                                "Range": f"bytes={byte_start}-{byte_end}",
                            },
                            allow_redirects=True,
                            timeout=300,
                            stream=True,
                        )

                        if resp.status_code not in (200, 206):
                            raise Exception(f"HTTP {resp.status_code}")

                        if not first_chunk_started[0]:
                            first_chunk_started[0] = True
                            await _update_progress(force=True)

                        # دانلود chunk به memory
                        chunk_data = b""
                        async for piece in resp.aiter_content():
                            if not piece:
                                continue
                            if active_downloads.get(dl_id, {}).get("cancelled"):
                                return False
                            chunk_data += piece

                        if len(chunk_data) != chunk_size:
                            raise Exception(f"Size mismatch: expected {chunk_size}, got {len(chunk_data)}")

                        # نوشتن به فایل
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
                        logger.warning(f"[DL-CARTOON] Worker {worker_id} chunk {c_idx} attempt {attempt+1} failed: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 * (attempt + 1))
                        else:
                            failed_chunks.append((c_idx, str(e)[:100]))
                            return False

                chunk_queue.task_done()

            return True

        # dl_id باید از بالا پاس داده بشه — اما این تابع standalone هست
        # پس یه متغیر global استفاده می‌کنیم
        # در واقع، این تابع باید dl_id رو به عنوان پارامتر بگیره
        # ولی برای سازگاری با API فعلی، از یه متغیر module-level استفاده می‌کنیم
        # TODO: بهبود API

        # اجرای worker ها
        try:
            results = await asyncio.gather(
                *[_download_worker(i) for i in range(num_workers)],
                return_exceptions=True,
            )

            try:
                await shared_session.close()
            except Exception:
                pass


            worker_failures = [r for r in results if r is not True and isinstance(r, bool) and not r]
            if worker_failures or failed_chunks:
                logger.warning(f"[DL-CARTOON] {len(worker_failures)} workers failed, {len(failed_chunks)} chunks failed")
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-CARTOON] Work-queue error: {e}", exc_info=True)
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
            logger.warning(f"[DL-CARTOON] Size mismatch: expected={content_length}, got={file_size}")

        elapsed = time.time() - start_time
        avg_speed = file_size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        logger.info(f"[DL-CARTOON] Multi-segment DONE | size={_format_size(file_size)} | time={elapsed:.1f}s | avg_speed={avg_speed:.1f} MB/s")
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-CARTOON] Multi-segment error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# متغیر module-level برای active_downloads (با bot.py سازگار)
active_downloads: dict = {}


async def download_cartoonporn_video(
    page_url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    cookies: Optional[dict] = None,
    dl_id: str = "",
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو از cartoonporn.

    استراتژی:
      1. single-connection با curl_cffi
      2. yt-dlp با --impersonate=chrome روی URL صفحه (fallback)

    Args:
        page_url: URL صفحه ویدیو (برای Referer)
        video_url: URL مستقیم ویدیو (با v-acctoken)
        filepath: مسیر ذخیره فایل
        progress_cb: callback برای گزارش پیشرفت
        cookies: کوکی‌های session (PHPSESSID, kt_acctoken, etc.)
        dl_id: download ID برای cancel support

    Returns:
        (success, error_message, file_size)
    """
    if not is_cartoonporn_url(page_url):
        return False, "URL host not allowed", 0

    if not video_url:
        return False, "Empty video URL", 0

    if progress_cb is None:
        async def _noop(msg: str) -> None:
            pass
        progress_cb = _noop

    if dl_id and dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}

    referer = page_url
    if not cookies:
        cookies = {}

    # ── روش 1: single-connection با curl_cffi ──
    # NOTE: multi-segment غیرفعال شد چون Range requests روی این CDN خراب کار می‌کنه
    if _check_curl_cffi():
        logger.info("[DL-CARTOON] Attempt 1: single-connection curl_cffi")
        success, error, size = await _download_single_curl_cffi(
            video_url, filepath, referer, cookies, progress_cb,
        )
        if success:
            return True, "", size
        if error == "HTTP_403":
            logger.info("[DL-CARTOON] 403 on CDN, refreshing cookies...")
            if progress_cb:
                await progress_cb("🔄 **Refreshing session...**")
            _, _, new_cookies, fetch_err = await _fetch_page_curl_cffi(page_url)
            if new_cookies:
                cookies = new_cookies
                if "v-acctoken" not in video_url:
                    html, _, _, _ = await _fetch_page_curl_cffi(page_url, cookies)
                    if html:
                        sources = _extract_video_sources(html)
                        if sources:
                            video_url = sources[0]["url"]
                            logger.info("[DL-CARTOON] Got fresh URL with v-acctoken")
            success, error, size = await _download_single_curl_cffi(
                video_url, filepath, referer, cookies, progress_cb,
            )
            if success:
                return True, "", size
        logger.info(f"[DL-CARTOON] Single-connection failed: {error}")
        _cleanup_file(filepath)

    # ── روش 2: yt-dlp با --impersonate ──
    if shutil.which("yt-dlp"):
        logger.info("[DL-CARTOON] Attempt 2: yt-dlp on page URL")
        if progress_cb:
            await progress_cb("📥 **Fallback: yt-dlp...**")
        success, error, size = await _download_with_ytdlp(
            page_url, filepath, progress_cb,
        )
        if success:
            return True, "", size
        logger.info(f"[DL-CARTOON] yt-dlp failed: {error}")
        _cleanup_file(filepath)

    return False, error or "All download methods failed", 0


async def _download_single_curl_cffi(
    url: str,
    filepath: str,
    referer: str,
    cookies: dict,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود با single connection (fallback)."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        await progress_cb("📥 **Downloading (single connection)...**")

        async with AsyncSession() as session:
            # set cookies
            for name, value in cookies.items():
                try:
                    session.cookies.set(name, value)
                except Exception:
                    pass

            resp = await session.get(
                url,
                impersonate="chrome",
                headers={
                    "Referer": referer,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                allow_redirects=True,
                timeout=600,
                stream=True,
            )

            if resp.status_code == 403:
                return False, "HTTP_403", 0
            if resp.status_code not in (200, 206):
                return False, f"HTTP {resp.status_code}", 0

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > MAX_DOWNLOAD_SIZE:
                return False, f"File too large: {_format_size(content_length)}", 0

            downloaded = 0
            start_time = time.time()
            last_update = 0.0

            async with aiofiles.open(filepath, "wb") as f:
                async for chunk in resp.aiter_content():
                    if not chunk:
                        continue
                    if active_downloads.get("", {}).get("cancelled"):
                        _cleanup_file(filepath)
                        return False, "Cancelled by user", 0

                    await f.write(chunk)
                    downloaded += len(chunk)

                    if downloaded > MAX_DOWNLOAD_SIZE:
                        _cleanup_file(filepath)
                        return False, "Download exceeded size limit", 0

                    now = time.time()
                    if now - last_update >= PROGRESS_INTERVAL:
                        last_update = now
                        await progress_cb(
                            _format_progress(
                                downloaded, content_length, start_time, now
                            )
                        )

        size = os.path.getsize(filepath)
        if size < MIN_VALID_VIDEO_SIZE:
            _cleanup_file(filepath)
            return False, f"File too small ({size} bytes)", 0

        logger.info(f"[DL-CARTOON] Single DONE | size={_format_size(size)}")
        return True, "", size

    except Exception as e:
        logger.error(f"[DL-CARTOON] Single-connection error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


async def _download_with_ytdlp(
    page_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    quality_key: str = "",
) -> Tuple[bool, str, int]:
    """دانلود با yt-dlp با --impersonate=chrome."""
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    has_curl_cffi = _check_curl_cffi()
    await progress_cb("📥 **شروع دانلود (yt-dlp)...**")

    # format selector
    format_selector = "best"
    if quality_key == "720p":
        format_selector = "720p/best"
    elif quality_key == "480p":
        format_selector = "480p/best"
    elif quality_key == "1080p":
        format_selector = "1080p/best"

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--progress",
            "--newline",
            "--no-check-certificates",
            "-f", format_selector,
            "--concurrent-fragments", "8",
            "--retries", "10",
            "--fragment-retries", "10",
            "--buffer-size", "16K",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "-o", filepath,
        ]

        if has_curl_cffi:
            cmd.extend(["--impersonate", "chrome"])

        cmd.append(page_url)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        last_update = 0.0
        while True:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=300
                )
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

        logger.info(f"[DL-CARTOON] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-CARTOON] yt-dlp error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


def _parse_ytdlp_progress(text: str) -> Optional[str]:
    """پارس خط progress yt-dlp."""
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
        f"📥 **Downloading...**\n"
        f"`[{bar}]`\n"
        f"💾 {total}  •  ⚡ {speed}\n"
        f"📊 {pct}%  •  ⏱ ETA: {eta}"
    )


def _extract_ytdlp_error(stderr: str) -> str:
    """استخراج پیام خطای اصلی از stderr yt-dlp."""
    if not stderr:
        return "Unknown error"

    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("ERROR:"):
            return line[6:].strip()[:200]

    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    if lines:
        return lines[-1][:200]

    return "Unknown error"


def _find_output_file(filepath: str) -> Optional[str]:
    """پیدا کردن فایل خروجی yt-dlp."""
    if os.path.exists(filepath):
        return filepath

    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = base + ext
        if os.path.exists(candidate):
            return candidate

    return None


# ─── Public API (سازگار با bot architecture) ──────────────────────────────


async def download_cartoonporn_direct(
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
        qualities, title, info = await extract_cartoonporn_qualities(
            url, progress_cb
        )
        if not qualities:
            return False, title or "Extraction failed", 0

        # انتخاب کیفیت
        selected = None
        # اولوية با quality_key
        for q in qualities:
            if q.get("quality_key") == quality:
                selected = q
                break
        # fallback: high → بالاترین، low → پایین‌ترین
        if not selected:
            if quality in ("high", "best"):
                selected = qualities[0]
            elif quality in ("low", "worst"):
                selected = qualities[-1]
            else:
                selected = qualities[0]

        video_url = selected["url"]
        quality_key = selected.get("quality_key", quality)
        cookies = info.get("cookies", {})
    else:
        quality_key = quality
        # اگه video_url داده شده ولی cookies نه، باید fetch کنیم
        qualities, title, info = await extract_cartoonporn_qualities(
            url, progress_cb
        )
        cookies = info.get("cookies", {}) if info else {}

    return await download_cartoonporn_video(
        url, video_url, filepath, progress_cb,
        cookies=cookies, dl_id=dl_id,
    )
