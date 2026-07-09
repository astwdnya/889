"""
sleazyneasy_handler.py
──────────────────────
استخراج و دانلود ویدیو از sleazyneasy.com (با focus روی سرعت بالا)

روش کار (بر اساس تحلیل واقعی صفحه):
  - سایت پشت Cloudflare نیست (aiohttp مستقیم کار می‌کنه)
  - Player: KT Player (مثل fetishshrine، pornwhite، babestube)
  - URL ویدیو از /get_file/1/HASH/.../FILEID.mp4/?v-acctoken=TOKEN میان
  - کوکی kt_acctoken و PHPSESSID برای session persistence لازمه
  - URL امضا‌دار با TTL کوتاه (باید هر بار fetch کنی)
  - سرور: CDN77-Turbo (CDN پرسرعت!)
  - سرور از Range request پشتیبانی می‌کنه (HTTP 206, Accept-Ranges: bytes)
  - yt-dlp با extractor=generic کار می‌کنه

کیفیت‌ها:
  sleazyneasy از flashvars استفاده می‌کنه:
    - video_url: 'https://...'         → کیفیت اصلی
    - video_url_fhd: '1'               → flag که می‌گه ویدیو FHD هست
  نکته: sleazyneasy فقط یه کیفیت ارائه می‌ده (default = 720p یا FHD).

استراتژی دانلود (با focus روی سرعت بالا):
  1. fetch صفحه با aiohttp (سریع)
  2. fallback به curl_cffi با impersonate=chrome
  3. استخراج video_url از flashvars block
  4. multi-segment download با 32 workers (CDN77 سرعت بالایی داره)
  5. fallback به single-connection
  6. fallback به yt-dlp روی URL صفحه

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

logger = logging.getLogger("SleazyNeasyHandler")

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
PROGRESS_INTERVAL = 1.0  # بیشتر از بقیه (1.0s) برای آپدیت سریع‌تر
CHUNK_SIZE = 1024 * 1024  # 1 MB (برای single connection)
MAX_RETRIES = 3
RETRY_DELAY = 2.0
MULTI_SEGMENT_MIN_SIZE = 5 * 1024 * 1024  # 5 MB

# ─── تنظیمات سرعت بالا (مهم) ───────────────────────────────────────────────
# CDN77 سرعت بالایی داره — تعداد workers بیشتر + chunk size بزرگ‌تر
MULTI_SEGMENT_WORKERS = 32  # به جای 16 — برای سرعت بیشتر
MULTI_SEGMENT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB (به جای 5 MB)
CONNECTOR_LIMIT = 50
CONNECTOR_LIMIT_PER_HOST = 50

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset({
    "sleazyneasy.com",
    "www.sleazyneasy.com",
    "m.sleazyneasy.com",
})

# CDN مجاز (برای screenshot/preview)
_ALLOWED_CDN_HOSTS = frozenset({
    "mjedge.net",
    "cdn.sleazyneasy.com",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_sleazyneasy_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به sleazyneasy هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".sleazyneasy.com")
    except Exception:
        return False


def _is_main_video_url(url: str) -> bool:
    """بررسی اینکه URL یه ویدیوی اصلی هست (نه preview/screenshot)."""
    url_lower = url.lower()
    if "preview" in url_lower:
        return False
    if "_preview" in url_lower:
        return False
    if "screenshot" in url_lower:
        return False
    if "/contents/videos_screenshots/" in url_lower:
        return False
    if "stats.php" in url_lower:
        return False
    if "/get_file/" not in url_lower and ".mp4" not in url_lower:
        return False
    if ".mp4" not in url_lower and "/get_file/" not in url_lower:
        return False
    return True


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
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
    url = url.rstrip("',\"")
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
    return f"📥 **Downloading...**\n💾 {dl_mb:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"


def _check_curl_cffi() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


# ─── Extraction ────────────────────────────────────────────────────────────


def _extract_title(html: str) -> str:
    """استخراج عنوان ویدیو."""
    # روش 1: از flashvars
    flashvars = _extract_flashvars(html)
    if flashvars.get("video_title"):
        title = flashvars["video_title"].strip()
        title = re.sub(
            r"\s*[-|@]\s*(?:sleazyneasy\.com|SleazyNEasy)\s*$",
            "", title, flags=re.IGNORECASE,
        )
        if title:
            return title

    # روش 2: og:title
    m = re.search(r'(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if not m:
        m = re.search(r'content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:title["\']', html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|@]\s*(?:sleazyneasy\.com|SleazyNEasy)\s*$", "", title, flags=re.IGNORECASE)
        return title

    # روش 3: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|@]\s*(?:sleazyneasy\.com|SleazyNEasy)\s*$", "", title, flags=re.IGNORECASE)
        return title or "Untitled"

    return "Untitled"


def _extract_thumbnail(html: str) -> str:
    m = re.search(r'(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


def _extract_duration(html: str) -> Optional[int]:
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
        return total if total > 0 else None
    m = re.search(r'data-duration=["\'](\d+)["\']', html, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def _extract_flashvars(html: str) -> dict:
    """استخراج flashvars block از HTML (KT Player)."""
    flashvars = {}
    fv_match = re.search(r'var\s+flashvars\s*=\s*\{([^}]+(?:\{[^}]*\}[^{}]*)*)\}', html, re.DOTALL)
    if not fv_match:
        return flashvars

    block = fv_match.group(0)

    # video_title special extraction (handles ' in title)
    title_match = re.search(r"video_title\s*:\s*'((?:[^'\\]|\\.)*)'", block)
    if title_match:
        title_val = title_match.group(1).replace("\\'", "'").replace("\\/", "/").replace("&amp;", "&")
        flashvars["video_title"] = title_val
        block_for_pairs = block[:title_match.start()] + block[title_match.end():]
    else:
        block_for_pairs = block

    pairs = re.findall(r"(\w+)\s*:\s*'([^']*)'", block_for_pairs)
    pairs += re.findall(r'(\w+)\s*:\s*"([^"]*)"', block_for_pairs)
    pairs += re.findall(r"(\w+)\s*:\s*([0-9]+)", block_for_pairs)

    for k, v in pairs:
        if k.startswith("//") or k.startswith("/*"):
            continue
        v_decoded = v.replace("\\/", "/").replace("&amp;", "&")
        flashvars[k] = v_decoded

    return flashvars


def _extract_video_sources(html: str) -> List[dict]:
    """
    استخراج URL های ویدیو اصلی از HTML.

    اولویت 1: video_url از flashvars (KT Player)
    اولویت 2: URL های /get_file/ با v-acctoken
    اولویت 3: URL های /get_file/ بدون v-acctoken

    Returns:
        list of dicts: [{label, url, height, quality_key, method, is_hd}, ...]
    """
    sources = []
    seen_urls = set()

    # ─── روش 1: از flashvars ───
    flashvars = _extract_flashvars(html)

    if flashvars.get("video_url"):
        url = _clean_url(flashvars["video_url"])
        if _is_main_video_url(url) and url not in seen_urls:
            seen_urls.add(url)
            is_hd = flashvars.get("video_url_fhd") == "1" or flashvars.get("video_url_hd") == "1"
            url_lower = url.lower()
            if "_1080p" in url_lower:
                quality_text = "1080p"
                height = 1080
                is_hd = True
            elif "_720p" in url_lower:
                quality_text = "720p"
                height = 720
                is_hd = True
            elif "_480p" in url_lower:
                quality_text = "480p"
                height = 480
            else:
                # default — اگه video_url_fhd=1 باشه، یعنی FHD (1080p)
                if is_hd:
                    quality_text = "1080p"
                    height = 1080
                else:
                    quality_text = "default"
                    height = 720

            label = f"📺 MP4 {quality_text}" if quality_text != "default" else "📺 MP4 (default)"
            sources.append({
                "label": label,
                "url": url,
                "height": height,
                "quality_key": quality_text.lower().replace(" ", ""),
                "method": "flashvars",
                "is_hd": is_hd,
            })
            logger.info("Found video_url from flashvars (%s): %s", quality_text, url[:100])

    # الگوی alt (برای آینده، اگه سایت اضافه کرد)
    alt_keys_patterns = [
        ("video_alt_url", "video_alt_url_text", "video_alt_url_hd"),
        ("video_alt_url2", "video_alt_url2_text", "video_alt_url2_hd"),
    ]
    for url_key, text_key, hd_key in alt_keys_patterns:
        alt_url = flashvars.get(url_key)
        if not alt_url:
            continue
        url = _clean_url(alt_url)
        if not _is_main_video_url(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        quality_text = flashvars.get(text_key, "")
        is_hd = flashvars.get(hd_key) == "1"
        url_lower = url.lower()
        if "_1080p" in url_lower:
            quality_text = quality_text or "1080p"
            height = 1080
            is_hd = True
        elif "_720p" in url_lower:
            quality_text = quality_text or "720p"
            height = 720
            is_hd = True
        elif "_480p" in url_lower:
            quality_text = quality_text or "480p"
            height = 480
        elif quality_text:
            height = int(re.match(r'(\d+)', quality_text).group(1)) if re.match(r'(\d+)', quality_text) else 720
        else:
            height = 720
            quality_text = quality_text or "alt"
        quality_key = quality_text.lower().replace(" ", "") if quality_text else "alt"
        label = f"📺 MP4 {quality_text}" if quality_text and quality_text != "alt" else "📺 MP4 (alt)"
        sources.append({
            "label": label,
            "url": url,
            "height": height,
            "quality_key": quality_key,
            "method": "flashvars_alt",
            "is_hd": is_hd,
        })

    # ─── روش 2: URL های /get_file/ با v-acctoken ───
    vacctoken_pattern = re.compile(
        r'(https?://[^\s"\'<>\)\]]+?/get_file/[^\s"\'<>\)\]]+?\.mp4[^\s"\'<>\)\]]*?\?v-acctoken=[a-zA-Z0-9+/=_-]+)',
        re.IGNORECASE,
    )
    for m in vacctoken_pattern.finditer(html):
        url = _clean_url(m.group(1))
        if not _is_main_video_url(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        url_lower = url.lower()
        if "_1080p" in url_lower:
            label, height, quality_key, is_hd = "📺 MP4 1080p", 1080, "1080p", True
        elif "_720p" in url_lower:
            label, height, quality_key, is_hd = "📺 MP4 720p", 720, "720p", True
        elif "_480p" in url_lower:
            label, height, quality_key, is_hd = "📺 MP4 480p", 480, "480p", False
        else:
            label, height, quality_key, is_hd = "📺 MP4 (default)", 720, "default", False
        sources.append({
            "label": label, "url": url, "height": height,
            "quality_key": quality_key, "method": "v-acctoken", "is_hd": is_hd,
        })

    # ─── روش 3: URL های /get_file/ بدون v-acctoken ───
    getfile_pattern = re.compile(
        r'(https?://[^\s"\'<>\)\]]+?/get_file/[^\s"\'<>\)\]]+?\.mp4)(?:[/?\s"\'<>\)\]]|$)',
        re.IGNORECASE,
    )
    for m in getfile_pattern.finditer(html):
        url = _clean_url(m.group(1))
        if not _is_main_video_url(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        url_lower = url.lower()
        if "_1080p" in url_lower:
            label, height, quality_key, is_hd = "📺 MP4 1080p", 1080, "1080p", True
        elif "_720p" in url_lower:
            label, height, quality_key, is_hd = "📺 MP4 720p", 720, "720p", True
        elif "_480p" in url_lower:
            label, height, quality_key, is_hd = "📺 MP4 480p", 480, "480p", False
        else:
            label, height, quality_key, is_hd = "📺 MP4 (default)", 720, "default", False
        sources.append({
            "label": label, "url": url, "height": height,
            "quality_key": quality_key, "method": "get_file", "is_hd": is_hd,
        })

    # مرتب‌سازی: بالاترین کیفیت اول
    sources.sort(key=lambda q: q.get("height", 0), reverse=True)
    return sources


# ─── Fetch Page ───────────────────────────────────────────────────────────


async def _fetch_page(url, jar=None):
    """fetch صفحه. اول aiohttp (سریع)، بعد curl_cffi (fallback)."""
    # ── روش 1: aiohttp ──
    try:
        local_jar = jar or CookieJar(unsafe=True)
        timeout = ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(timeout=timeout, headers=_DEFAULT_HEADERS, cookie_jar=local_jar) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="replace")
                    if "video_url" in html or "flashvars" in html:
                        logger.info("Page fetched via aiohttp, size=%d", len(html))
                        return html, local_jar, ""
                    else:
                        logger.warning("aiohttp: 200 ولی video_url پیدا نشد")
                logger.warning("aiohttp fetch: HTTP %s", resp.status)
    except Exception as e:
        logger.warning(f"aiohttp fetch error: {e}")

    # ── روش 2: curl_cffi (fallback) ──
    if _check_curl_cffi():
        try:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession() as session:
                resp = await session.get(url, impersonate="chrome", headers=_DEFAULT_HEADERS, allow_redirects=True, timeout=30)
                if resp.status_code == 200 and resp.text:
                    text = resp.text
                    if "video_url" in text or "flashvars" in text:
                        logger.info("Page fetched via curl_cffi, size=%d", len(text))
                        # استخراج کوکی‌ها
                        cookies_dict = {}
                        try:
                            for cookie in session.cookies.jar:
                                cookies_dict[cookie.name] = cookie.value
                        except Exception:
                            try:
                                for cookie in session.cookies:
                                    if hasattr(cookie, "name") and hasattr(cookie, "value"):
                                        cookies_dict[cookie.name] = cookie.value
                            except Exception:
                                pass
                        local_jar = CookieJar(unsafe=True)
                        for k, v in cookies_dict.items():
                            try:
                                local_jar.update_cookies({k: v})
                            except Exception:
                                pass
                        return text, local_jar, ""
                    else:
                        logger.warning("curl_cffi: 200 ولی video_url پیدا نشد")
                logger.warning("curl_cffi fetch: HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning(f"curl_cffi fetch error: {e}")

    return None, jar, "Failed to fetch page"


# ─── Main API: extract qualities ──────────────────────────────────────────


async def extract_sleazyneasy_qualities(url, progress_cb=None):
    """استخراج کیفیت‌های ویدیو."""
    if not is_sleazyneasy_url(url):
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
        logger.error("No video sources found in page")
        return [], "URL ویدیو در صفحه پیدا نشد", {}

    cookies = {}
    if jar:
        for cookie in jar:
            cookies[cookie.key] = cookie.value

    logger.info("Found %d video sources (cookies: %s)", len(sources), list(cookies.keys()) if cookies else "none")

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
        "flashvars": _extract_flashvars(html),
    }


# ─── Download: Multi-segment (fast) ───────────────────────────────────────


active_downloads: dict = {}


async def _download_multi_segment(direct_url, filepath, referer, cookies, progress_cb, dl_id="", num_workers=MULTI_SEGMENT_WORKERS):
    """
    دانلود چند تیکه‌ای با work-queue pattern — OPTIMIZED برای سرعت بالا.

    CDN77 سرعت بالایی داره، پس از 32 workers و 10MB chunks استفاده می‌کنیم.
    """
    try:
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

        # probe با Range اگه HEAD کار نکرد
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
        await progress_cb(f"📥 **Downloading...**\n💾 Size: {total_mb:.1f} MB\n🔥 {num_workers} parallel workers")

        CHUNK_SIZE_BYTES = MULTI_SEGMENT_CHUNK_SIZE
        chunks = []
        offset = 0
        chunk_idx = 0
        while offset < content_length:
            end = min(offset + CHUNK_SIZE_BYTES - 1, content_length - 1)
            chunks.append((chunk_idx, offset, end))
            offset = end + 1
            chunk_idx += 1

        total_chunks = len(chunks)
        logger.info(f"[DL-SLEAZY] Work-queue: {total_chunks} chunks, {num_workers} workers, total={content_length}")

        try:
            with open(filepath, "wb") as f:
                f.truncate(content_length)
        except Exception as e:
            logger.warning(f"Could not pre-allocate file: {e}")

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

        async def _update_progress(force=False):
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
        shared_timeout = ClientTimeout(total=600, connect=30, sock_read=120)
        connector = TCPConnector(limit=CONNECTOR_LIMIT, limit_per_host=CONNECTOR_LIMIT_PER_HOST, keepalive_timeout=60, enable_cleanup_closed=True)
        shared_session = aiohttp.ClientSession(timeout=shared_timeout, headers=headers, cookies=cookies, connector=connector)

        async def _download_worker(worker_id):
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
                        async with shared_session.get(direct_url, headers={"Range": f"bytes={byte_start}-{byte_end}"}, allow_redirects=True) as resp:
                            if resp.status not in (200, 206):
                                raise Exception(f"HTTP {resp.status}")
                            if not first_chunk_started[0]:
                                first_chunk_started[0] = True
                                await _update_progress(force=True)
                            chunk_data = bytearray()
                            async for piece in resp.content.iter_chunked(CHUNK_SIZE):
                                if not piece:
                                    continue
                                if active_downloads.get(dl_id, {}).get("cancelled"):
                                    return False
                                chunk_data.extend(piece)
                            if len(chunk_data) != chunk_size:
                                raise Exception(f"Size mismatch: expected {chunk_size}, got {len(chunk_data)}")
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
                        logger.warning(f"[DL-SLEAZY] W{worker_id} c{c_idx} attempt {attempt+1} failed: {e}")
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 * (attempt + 1))
                        else:
                            failed_chunks.append((c_idx, str(e)[:100]))
                            return False
                chunk_queue.task_done()
            return True

        try:
            results = await asyncio.gather(*[_download_worker(i) for i in range(num_workers)], return_exceptions=True)
            try:
                await shared_session.close()
            except Exception:
                pass

            if active_downloads.get(dl_id, {}).get("cancelled"):
                _cleanup_file(filepath)
                return False, "Cancelled by user", 0

            worker_failures = [r for r in results if r is not True and isinstance(r, bool) and not r]
            if worker_failures or failed_chunks:
                logger.warning(f"[DL-SLEAZY] {len(worker_failures)} workers failed, {len(failed_chunks)} chunks failed")
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-SLEAZY] Work-queue error: {e}", exc_info=True)
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

        elapsed = time.time() - start_time
        avg_speed = file_size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        logger.info(f"[DL-SLEAZY] Multi-segment DONE | size={_format_size(file_size)} | time={elapsed:.1f}s | avg_speed={avg_speed:.1f} MB/s")
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-SLEAZY] Multi-segment error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Download: Single connection (fallback) ───────────────────────────────


async def _download_single_aiohttp(url, filepath, referer, cookies, progress_cb, dl_id=""):
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
                                    await progress_cb(_format_progress(downloaded, content_length, start_time, now))
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


async def _download_with_ytdlp(url, filepath, progress_cb, quality_key=""):
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0
    has_curl_cffi = _check_curl_cffi()
    await progress_cb("📥 **Fallback: yt-dlp...**")
    format_selector = "best"
    if quality_key in ("720p", "480p", "1080p", "360p"):
        format_selector = f"{quality_key}/best"
    try:
        cmd = [
            "yt-dlp", "--no-warnings", "--progress", "--newline",
            "--no-check-certificates", "-f", format_selector,
            "--concurrent-fragments", "16",
            "--retries", "10", "--fragment-retries", "10",
            "--buffer-size", "16K",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "--add-header", f"Referer:https://sleazyneasy.com/",
            "-o", filepath,
        ]
        if has_curl_cffi:
            cmd.extend(["--impersonate", "chrome"])
        cmd.append(url)
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
        logger.info(f"[DL-SLEAZY] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-SLEAZY] yt-dlp error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


def _parse_ytdlp_progress(text):
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
    return f"📥 **Downloading...**\n`[{bar}]`\n💾 {total}  •  ⚡ {speed}\n📊 {pct}%  •  ⏱ ETA: {eta}"


def _extract_ytdlp_error(stderr):
    if not stderr:
        return "Unknown error"
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("ERROR:"):
            return line[6:].strip()[:200]
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    return lines[-1][:200] if lines else "Unknown error"


def _find_output_file(filepath):
    if os.path.exists(filepath):
        return filepath
    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = base + ext
        if os.path.exists(candidate):
            return candidate
    return None


# ─── Public API ────────────────────────────────────────────────────────────


async def download_sleazyneasy_video(page_url, video_url, filepath, progress_cb=None, cookies=None, dl_id=""):
    """دانلود ویدیو از sleazyneasy."""
    if not is_sleazyneasy_url(page_url):
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

    # ── روش 1: multi-segment (با 32 workers برای سرعت بالا) ──
    logger.info(f"[DL-SLEAZY] Attempt 1: multi-segment ({MULTI_SEGMENT_WORKERS} workers)")
    success, error, size = await _download_multi_segment(video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id)
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0
    if error == "HTTP_403":
        logger.info("[DL-SLEAZY] 403, refreshing session...")
        if progress_cb:
            await progress_cb("🔄 **Refreshing session...**")
        _, jar, _ = await _fetch_page(page_url)
        if jar:
            new_cookies = {}
            for c in jar:
                new_cookies[c.key] = c.value
            cookies.update(new_cookies)
            qualities, _, info = await extract_sleazyneasy_qualities(page_url, progress_cb=None)
            if qualities:
                video_url = qualities[0]["url"]
                logger.info("[DL-SLEAZY] Got fresh URL")
        success, error, size = await _download_multi_segment(video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id)
        if success:
            return True, "", size
    logger.info(f"[DL-SLEAZY] Multi-segment failed: {error}")
    _cleanup_file(filepath)

    # ── روش 2: single-connection ──
    logger.info("[DL-SLEAZY] Attempt 2: single-connection")
    success, error, size = await _download_single_aiohttp(video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id)
    if success:
        return True, "", size
    logger.info(f"[DL-SLEAZY] Single failed: {error}")
    _cleanup_file(filepath)

    # ─ـ روش 3: yt-dlp ──
    logger.info("[DL-SLEAZY] Attempt 3: yt-dlp on page URL")
    success, error, size = await _download_with_ytdlp(page_url, filepath, progress_cb)
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


# ─── Wrapper (سازگار با bot architecture) ─────────────────────────────────


async def download_sleazyneasy_direct(url, filepath, progress_cb=None, video_url="", quality="high", dl_id=""):
    """Wrapper برای سازگاری با bot architecture."""
    if not video_url:
        qualities, title, info = await extract_sleazyneasy_qualities(url, progress_cb)
        if not qualities:
            return False, title or "Extraction failed", 0
        selected = None
        for q in qualities:
            if q.get("quality_key") == quality:
                selected = q
                break
        if not selected:
            if quality in ("high", "best"):
                hd = [q for q in qualities if q.get("is_hd")]
                selected = hd[0] if hd else qualities[0]
            elif quality in ("low", "worst"):
                selected = qualities[-1]
            else:
                selected = qualities[0]
        video_url = selected["url"]
        cookies = info.get("cookies", {})
    else:
        qualities, title, info = await extract_sleazyneasy_qualities(url, progress_cb)
        cookies = info.get("cookies", {}) if info else {}
    return await download_sleazyneasy_video(url, video_url, filepath, progress_cb, cookies=cookies, dl_id=dl_id)
