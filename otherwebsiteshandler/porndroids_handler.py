"""
porndroids_handler.py
─────────────────────
استخراج و دانلود ویدیو از porndroids.com (با focus روی سرعت بالا)

روش کار (بر اساس تحلیل واقعی صفحه با analyze_porndroids.py):
  - سایت پشت Cloudflare نیست (aiohttp مستقیم کار می‌کنه)
  - Player: Video.js (HTML5 native)
  - URL ویدیو از <source> tag داخل <video> میان
  - URL از cdnst.porndroids.com با ساختار:
      https://cdnst.porndroids.com/videos/HASH.mp4?secure=TOKEN%3D%3D%2CTIMESTAMP
  - URL دارای secure token و expiry timestamp (TTL کوتاه، باید هر بار fetch کنی)
  - سرور: CDN77-Turbo (CDN77 یه CDN پرسرعت هست)
  - سرور از Range request پشتیبانی می‌کنه (HTTP 206, Accept-Ranges: bytes)
  - yt-dlp با extractor=generic کار می‌کنه
  - سایت فقط یه کیفیت ارائه می‌ده (از طریق <source> tag)

سرعت بالا (مهم):
  - CDN77 سرعت بالایی داره
  - multi-segment download با 32 workers (بیشتر از بقیه سایت‌ها)
  - chunk size بزرگ‌تر (10MB به جای 5MB) برای overhead کمتر
  - session اشتراکی با connection pooling
  - disable SSL verification برای سرعت بیشتر (در صورت نیاز)

استراتژی دانلود:
  1. fetch صفحه با aiohttp (سریع‌ترین)
  2. fallback به curl_cffi با impersonate=chrome
  3. استخراج URL از <source> tag (یا fallback به cdnst URLs)
  4. multi-segment download با 32 workers (سرور Range رو پشتیبانی می‌کنه)
  5. fallback به single-connection
  6. fallback به yt-dlp روی URL مستقیم

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
from aiohttp import ClientTimeout, CookieJar, TCPConnector

logger = logging.getLogger("PornDroidsHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ─── Constants ─────────────────────────────────────────────────────────────

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB (محدودیت تلگرام)
MIN_VALID_VIDEO_SIZE = 100 * 1024  # 100 KB
PROGRESS_INTERVAL = 1.0  # بیشتر از بقیه (1.0s به جای 1.5s) برای آپدیت سریع‌تر
CHUNK_SIZE = 1024 * 1024  # 1 MB (برای single connection)
MAX_RETRIES = 3
RETRY_DELAY = 2.0
MULTI_SEGMENT_MIN_SIZE = 5 * 1024 * 1024  # 5 MB

# ─── تنظیمات سرعت بالا (مهم) ───────────────────────────────────────────────
# تعداد workers بیشتر از بقیه سایت‌ها (CDN77 سرعت بالایی داره)
MULTI_SEGMENT_WORKERS = 32  # به جای 16 — برای سرعت بیشتر
# chunk size بزرگ‌تر برای overhead کمتر
MULTI_SEGMENT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB (به جای 5 MB)
# limit اتصالات همزمان در TCPConnector
CONNECTOR_LIMIT = 50  # بیشتر از تعداد workers
CONNECTOR_LIMIT_PER_HOST = 50

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset({
    "porndroids.com",
    "www.porndroids.com",
    "m.porndroids.com",
})

# CDN مجاز برای ویدیو اصلی
_ALLOWED_CDN_HOSTS = frozenset({
    "cdnst.porndroids.com",
    "porndroids0.pump-cdn.com",
    "porndroids1.pump-cdn.com",
    "porndroids2.pump-cdn.com",
    "porndroids3.pump-cdn.com",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_porndroids_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به porndroids هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".porndroids.com")
    except Exception:
        return False


def _is_main_video_url(url: str) -> bool:
    """بررسی اینکه URL یه ویدیوی اصلی هست (نه preview/screenshot/sprite)."""
    url_lower = url.lower()
    # فیلتر preview/sprite
    if "preview" in url_lower:
        return False
    if "sprite" in url_lower:
        return False
    if "/thumbs/" in url_lower:
        return False
    # باید .mp4 باشه
    if ".mp4" not in url_lower:
        return False
    # باید از host مجاز باشه
    try:
        host = urlparse(url).hostname or ""
        if host not in _ALLOWED_HOSTS and host not in _ALLOWED_CDN_HOSTS:
            if not host.endswith(".porndroids.com") and not host.endswith(".pump-cdn.com"):
                return False
    except Exception:
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
    url = url.rstrip("',\"")
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
        # حذف " - PORNDROIDS.COM" یا " - Porndroids"
        title = re.sub(
            r"\s*[-|]\s*(?:PORNDROIDS\.COM|Porndroids)\s*$",
            "", title, flags=re.IGNORECASE,
        )
        return title

    # روش 2: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(
            r"\s*[-|]\s*(?:PORNDROIDS\.COM|Porndroids)\s*$",
            "", title, flags=re.IGNORECASE,
        )
        return title or "Untitled"

    return "Untitled"


def _extract_thumbnail(html: str) -> str:
    """استخراج thumbnail از og:image یا video poster."""
    # روش 1: og:image
    m = re.search(
        r'(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    # روش 2: poster از <video> tag
    m = re.search(r'<video[^>]+poster=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return ""


def _extract_duration(html: str) -> Optional[int]:
    """استخراج مدت ویدیو از data-duration یا JSON-LD."""
    # روش 1: data-duration از <video> tag
    m = re.search(r'data-duration=["\'](\d+)["\']', html, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # روش 2: JSON-LD
    m = re.search(r'"duration"\s*:\s*"(PT[^"]+)"', html)
    if m:
        duration_str = m.group(1)
        h = re.search(r'(\d+)H', duration_str)
        m_min = re.search(r'(\d+)M', duration_str)
        s = re.search(r'(\d+)S', duration_str)
        total = 0
        if h: total += int(h.group(1)) * 3600
        if m_min: total += int(m_min.group(1)) * 60
        if s: total += int(s.group(1))
        if total > 0:
            return total

    return None


def _extract_video_sources(html: str) -> List[dict]:
    """
    استخراج URL های ویدیو اصلی از HTML.

    اولویت 1: <source> tag داخل <video> (مهم‌ترین — منبع اصلی porndroids)
    اولویت 2: data-video attribute
    اولویت 3: cdnst.porndroids.com URLs از HTML

    Returns:
        list of dicts: [{label, url, height, quality_key, method}, ...]
    """
    sources = []
    seen_urls = set()

    # ─── روش 1: <source> tag داخل <video> ───
    # پیدا کردن video tag اصلی (نه logo)
    # porndroids دو تا <video> tag داره: یکی برای logo (با style="display:none")، یکی برای ویدیو اصلی
    # ویدیو اصلی معمولا id="video-js" یا class شامل "video-js" داره
    
    # روش 1a: پیدا کردن video tag با id="video-js" یا class video-js
    video_blocks = []
    # الگوی video tag با id یا class شامل video-js
    for vm in re.finditer(
        r'<video\b[^>]*(?:id=["\']video-js["\']|class=["\'][^"\']*video-js[^"\']*["\'])[^>]*>(.*?)</video>',
        html, re.IGNORECASE | re.DOTALL,
    ):
        video_blocks.append(vm.group(0))
    
    # روش 1b: اگه پیدا نشد، همه video tag ها رو امتحان کن (به‌جز display:none)
    if not video_blocks:
        for vm in re.finditer(r'<video\b[^>]*>(.*?)</video>', html, re.IGNORECASE | re.DOTALL):
            block = vm.group(0)
            # فیلتر video tag های display:none (logo)
            if 'display: none' in block.lower() or 'display:none' in block.lower():
                continue
            # اگه source با src (نه srcset) داره، یعنی ویدیو هست
            if '<source' in block and 'src=' in block:
                video_blocks.append(block)
    
    # روش 1c: اگه بازم پیدا نشد، همه video tag ها رو بگیر
    if not video_blocks:
        for vm in re.finditer(r'<video\b[^>]*>(.*?)</video>', html, re.IGNORECASE | re.DOTALL):
            video_blocks.append(vm.group(0))
    
    for video_block in video_blocks:
        video_inner = video_block
        # پیدا کردن <source> tag ها داخل video
        for sm in re.finditer(
            r'<source\b([^>]*)>',
            video_inner, re.IGNORECASE,
        ):
            attrs_str = sm.group(1)
            attrs = {}
            for am in re.finditer(r'([\w-]+)\s*=\s*"([^"]*)"', attrs_str):
                attrs[am.group(1).lower()] = am.group(2)
            for am in re.finditer(r"([\w-]+)\s*=\s*'([^']*)'", attrs_str):
                attrs[am.group(1).lower()] = am.group(2)

            src = attrs.get("src", "")
            if not src:
                continue

            # فیلتر source های غیر ویدیویی (مثل logo svg)
            srcset = attrs.get("srcset", "")
            media = attrs.get("media", "")
            if srcset or media:
                continue  # این logo هست

            url = _clean_url(src)
            if not _is_main_video_url(url):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # porndroids فقط یه کیفیت داره (default)
            url_lower = url.lower()
            if "_1080p" in url_lower:
                label = "📺 MP4 1080p"
                height = 1080
                quality_key = "1080p"
            elif "_720p" in url_lower:
                label = "📺 MP4 720p"
                height = 720
                quality_key = "720p"
            elif "_480p" in url_lower:
                label = "📺 MP4 480p"
                height = 480
                quality_key = "480p"
            else:
                label = "📺 MP4 (default)"
                height = 720  # فرض می‌کنیم default نزدیک 720p باشه
                quality_key = "default"

            sources.append({
                "label": label,
                "url": url,
                "height": height,
                "quality_key": quality_key,
                "method": "source_tag",
            })
            logger.info("Found video URL from <source> tag: %s", url[:100])

    # ─── روش 2: data-video attribute ───
    for m in re.finditer(r'data-video=["\']([^"\']+\.mp4[^"\']*)["\']', html, re.IGNORECASE):
        url = _clean_url(m.group(1))
        if not _is_main_video_url(url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        sources.append({
            "label": "📺 MP4 (default)",
            "url": url,
            "height": 720,
            "quality_key": "default",
            "method": "data_video",
        })
        logger.info("Found video URL from data-video attribute: %s", url[:100])

    # ─── روش 3: پیدا کردن cdnst.porndroids.com URLs ───
    cdnst_pattern = re.compile(
        r'(https?://cdnst\.porndroids\.com/[^\s"\'<>\)\]]+\.mp4[^\s"\'<>\)\]]*)',
        re.IGNORECASE,
    )
    for m in cdnst_pattern.finditer(html):
        url = _clean_url(m.group(1))
        if not _is_main_video_url(url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        sources.append({
            "label": "📺 MP4 (default)",
            "url": url,
            "height": 720,
            "quality_key": "default",
            "method": "cdnst",
        })
        logger.info("Found cdnst URL: %s", url[:100])

    # مرتب‌سازی: source_tag اول (بهترین)
    method_priority = {"source_tag": 0, "data_video": 1, "cdnst": 2}
    sources.sort(key=lambda q: method_priority.get(q.get("method", ""), 99))

    return sources


# ─── Fetch Page ───────────────────────────────────────────────────────────


async def _fetch_page(
    url: str,
    jar: Optional[CookieJar] = None,
) -> Tuple[Optional[str], Optional[CookieJar], str]:
    """
    fetch صفحه. اول aiohttp (سریع)، بعد curl_cffi (fallback).

    Returns:
        (html, cookie_jar, error_message)
    """
    # ── روش 1: aiohttp ──
    try:
        local_jar = jar or CookieJar(unsafe=True)
        timeout = ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(
            timeout=timeout, headers=_DEFAULT_HEADERS, cookie_jar=local_jar
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="replace")
                    # چک کن که source یا cdnst وجود داره (یعنی محتوای واقعی هست)
                    if "cdnst" in html or "<source" in html:
                        logger.info("Page fetched via aiohttp, size=%d", len(html))
                        return html, local_jar, ""
                    else:
                        logger.warning("aiohttp: 200 ولی video source پیدا نشد")
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
                    headers=_DEFAULT_HEADERS,
                    allow_redirects=True,
                    timeout=30,
                )
                if resp.status_code == 200 and resp.text:
                    text = resp.text
                    if "cdnst" in text or "<source" in text:
                        logger.info("Page fetched via curl_cffi, size=%d", len(text))
                        return text, None, ""
                    else:
                        logger.warning("curl_cffi: 200 ولی video source پیدا نشد")
                logger.warning("curl_cffi fetch: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning(f"curl_cffi fetch error: {e}")

    return None, jar, "Failed to fetch page"


# ─── Main API: extract qualities ──────────────────────────────────────────


async def extract_porndroids_qualities(
    url: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict]:
    """
    استخراج کیفیت‌های ویدیو.

    Args:
        url: URL صفحه ویدیو
        progress_cb: callback برای گزارش پیشرفت

    Returns:
        (qualities, title, info)
        qualities: list of dicts with keys: label, url, height, quality_key, method
        title: str
        info: dict with extra info (thumbnail, cookies, etc.)
    """
    if not is_porndroids_url(url):
        return [], "Invalid URL", {}

    if progress_cb:
        await progress_cb("🔄 **دریافت اطلاعات صفحه...**")

    jar = CookieJar(unsafe=True)
    html, jar, error = await _fetch_page(url, jar)

    if not html:
        logger.error("Failed to fetch page: %s", error)
        return [], f"خطا در دریافت صفحه: {error}", {}

    # استخراج اطلاعات
    title = _extract_title(html)
    thumbnail = _extract_thumbnail(html)
    duration = _extract_duration(html)
    sources = _extract_video_sources(html)

    if not sources:
        logger.error("No video sources found in page")
        return [], "URL ویدیو در صفحه پیدا نشد", {}

    # استخراج کوکی‌ها
    cookies = {}
    if jar:
        for cookie in jar:
            cookies[cookie.key] = cookie.value

    logger.info(
        "Found %d video sources (cookies: %s)",
        len(sources), list(cookies.keys()) if cookies else "none",
    )

    if progress_cb:
        labels = ", ".join(s["label"] for s in sources[:3])
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


# ─── Download: Multi-segment (fast) ────────────────────────────────────────


# متغیر module-level برای cancel support (با bot.py سازگار)
active_downloads: dict = {}


async def _download_multi_segment(
    direct_url: str,
    filepath: str,
    referer: str,
    cookies: dict,
    progress_cb: ProgressCallback,
    dl_id: str = "",
    num_workers: int = MULTI_SEGMENT_WORKERS,
) -> Tuple[bool, str, int]:
    """
    دانلود چند تیکه‌ای با work-queue pattern — OPTIMIZED برای سرعت بالا.
    
    تنظیمات سرعت بالا:
    - 32 workers (به جای 16)
    - 10MB chunks (به جای 5MB)
    - TCPConnector با connection pooling
    - session اشتراکی با keepalive
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
                        ct = r.headers.get("Content-Type", "")
                        if ct and not ct.startswith("video/"):
                            logger.warning("HEAD returned non-video content-type: %s", ct)
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
            f"📥 **Downloading...**\n💾 Size: {total_mb:.1f} MB\n🔥 {num_workers} parallel workers"
        )

        # ── Work-queue pattern ──
        # chunk size بزرگ‌تر برای overhead کمتر
        CHUNK_SIZE_BYTES = MULTI_SEGMENT_CHUNK_SIZE  # 10 MB per chunk
        chunks = []
        offset = 0
        chunk_idx = 0
        while offset < content_length:
            end = min(offset + CHUNK_SIZE_BYTES - 1, content_length - 1)
            chunks.append((chunk_idx, offset, end))
            offset = end + 1
            chunk_idx += 1

        total_chunks = len(chunks)
        logger.info(f"[DL-PORNDROIDS] Work-queue: {total_chunks} chunks, {num_workers} workers, total={content_length}")

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

        # session اشتراکی با TCPConnector برای connection pooling
        # این بخش مهم برای سرعت بالاست
        shared_timeout = ClientTimeout(total=600, connect=30, sock_read=120)
        connector = TCPConnector(
            limit=CONNECTOR_LIMIT,
            limit_per_host=CONNECTOR_LIMIT_PER_HOST,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
        )
        shared_session = aiohttp.ClientSession(
            timeout=shared_timeout,
            headers=headers,
            cookies=cookies,
            connector=connector,
        )

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

                            # استفاده از bytearray برای performance بهتر
                            chunk_data = bytearray()
                            async for piece in resp.content.iter_chunked(CHUNK_SIZE):
                                if not piece:
                                    continue
                                if active_downloads.get(dl_id, {}).get("cancelled"):
                                    return False
                                chunk_data.extend(piece)

                            if len(chunk_data) != chunk_size:
                                raise Exception(f"Size mismatch: expected {chunk_size}, got {len(chunk_data)}")

                            # نوشتن به فایل
                            async with file_write_lock:
                                async with aiofiles.open(filepath, "r+b") as f:
                                    await f.seek(byte_start)
                                    await f.write(bytes(chunk_data))

                            downloaded_bytes[c_idx] = chunk_size
                            async with progress_lock:
                                completed_chunks[0] += 1
                                await _update_progress()
                            break

                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"[DL-PORNDROIDS] W{worker_id} c{c_idx} attempt {attempt+1} failed: {e}")
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
                logger.warning(f"[DL-PORNDROIDS] {len(worker_failures)} workers failed, {len(failed_chunks)} chunks failed")
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-PORNDROIDS] Work-queue error: {e}", exc_info=True)
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
            logger.warning(f"[DL-PORNDROIDS] Size mismatch: expected={content_length}, got={file_size}")

        elapsed = time.time() - start_time
        avg_speed = file_size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        logger.info(f"[DL-PORNDROIDS] Multi-segment DONE | size={_format_size(file_size)} | time={elapsed:.1f}s | avg_speed={avg_speed:.1f} MB/s")
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-PORNDROIDS] Multi-segment error: {e}", exc_info=True)
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


# ─── Download: yt-dlp (fallback نهایی) ────────────────────────────────────


async def _download_with_ytdlp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود با yt-dlp روی URL مستقیم یا URL صفحه."""
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    has_curl_cffi = _check_curl_cffi()
    await progress_cb("📥 **Fallback: yt-dlp...**")

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--progress",
            "--newline",
            "--no-check-certificates",
            "-f", "best",
            "--concurrent-fragments", "16",  # بیشتر از بقیه برای سرعت
            "--retries", "10",
            "--fragment-retries", "10",
            "--buffer-size", "16K",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "--add-header", f"Referer:https://porndroids.com/",
            "-o", filepath,
        ]

        if has_curl_cffi:
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

        logger.info(f"[DL-PORNDROIDS] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-PORNDROIDS] yt-dlp error: {e}", exc_info=True)
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


# ─── Public API ────────────────────────────────────────────────────────────


async def download_porndroids_video(
    page_url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    cookies: Optional[dict] = None,
    dl_id: str = "",
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو از porndroids.

    استراتژی:
      1. multi-segment download با 32 workers (سرعت بالا)
      2. single-connection با aiohttp (fallback)
      3. yt-dlp روی URL صفحه (fallback نهایی)

    Args:
        page_url: URL صفحه ویدیو (برای Referer)
        video_url: URL مستقیم ویدیو (از cdnst.porndroids.com)
        filepath: مسیر ذخیره فایل
        progress_cb: callback برای گزارش پیشرفت
        cookies: کوکی‌های session (اختیاری)
        dl_id: download ID برای cancel support

    Returns:
        (success, error_message, file_size)
    """
    if not is_porndroids_url(page_url):
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

    # ── روش 1: multi-segment (با 32 workers برای سرعت بالا) ──
    logger.info(f"[DL-PORNDROIDS] Attempt 1: multi-segment ({MULTI_SEGMENT_WORKERS} workers)")
    success, error, size = await _download_multi_segment(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0
    logger.info(f"[DL-PORNDROIDS] Multi-segment failed: {error}")
    _cleanup_file(filepath)

    # ── روش 2: single-connection ──
    logger.info("[DL-PORNDROIDS] Attempt 2: single-connection")
    success, error, size = await _download_single_aiohttp(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    logger.info(f"[DL-PORNDROIDS] Single failed: {error}")
    _cleanup_file(filepath)

    # ── روش 3: yt-dlp ──
    logger.info("[DL-PORNDROIDS] Attempt 3: yt-dlp on page URL")
    success, error, size = await _download_with_ytdlp(
        page_url, filepath, progress_cb,
    )
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


# ─── Wrapper (سازگار با bot architecture) ─────────────────────────────────


async def download_porndroids_direct(
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
        quality: 'high', 'low', 'default' (porndroids فقط یه کیفیت داره)
        dl_id: download ID برای cancel

    Returns:
        (success, error_message, file_size)
    """
    if not video_url:
        qualities, title, info = await extract_porndroids_qualities(
            url, progress_cb
        )
        if not qualities:
            return False, title or "Extraction failed", 0

        # انتخاب کیفیت (porndroids معمولا فقط یه کیفیت داره)
        selected = None
        for q in qualities:
            if q.get("quality_key") == quality:
                selected = q
                break
        if not selected:
            if quality in ("high", "best", "default"):
                selected = qualities[0]
            elif quality in ("low", "worst"):
                selected = qualities[-1]
            else:
                selected = qualities[0]

        video_url = selected["url"]
        cookies = info.get("cookies", {})
    else:
        # اگه video_url داده شده ولی cookies نه، باید fetch کنیم
        qualities, title, info = await extract_porndroids_qualities(
            url, progress_cb
        )
        cookies = info.get("cookies", {}) if info else {}

    return await download_porndroids_video(
        url, video_url, filepath, progress_cb,
        cookies=cookies, dl_id=dl_id,
    )
