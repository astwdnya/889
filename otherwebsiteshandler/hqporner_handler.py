"""
hqporner_handler.py
───────────────────
استخراج و دانلود ویدیو از hqporner.com (desktop + mobile: m.hqporner.com)

روش کار (بر اساس تحلیل واقعی صفحه):

  ─── معماری دو لایه ───
  1. صفحه hqporner.com (یا m.hqporner.com) HTML رو برمی‌گردونه که شامل یه
     iframe به mydaddy.cc/video/{ID}/ هست.
  2. iframe mydaddy.cc یه HTML دیگه برمی‌گردونه که شامل تگ‌های <source> برای
     کیفیت‌های مختلف (360, 720, 1080) روی CDN bigcdn.cc هست.

  ─── ساختار URL ویدیو ───
  https://s28.bigcdn.cc/pubs/{HASH}/{QUALITY}.mp4
    - HASH: هر ویدیو یه hash منحصر‌به‌فرم داره (مثل 6a4fe3590a8479.42911783)
    - QUALITY: 360, 720, 1080
    - poster: {HASH}/main.jpg

  ─── Player ───
  Fluid Player (html5) — تگ <video id="flvv"> با چندتا <source>

  ─── سرور ───
  - m.hqporner.com: nginx/1.16.1 + PHP/5.3.3 (بدون Cloudflare!)
  - mydaddy.cc: nginx/1.20.1 + PHP/5.4.16 (بدون Cloudflare!)
  - s28.bigcdn.cc: nginx/1.20.1 (CDN ساده با Range support)

  ─── رفتار CDN ───
  - Accept-Ranges: bytes ✓ (multi-segment download کار می‌کنه)
  - CORS: access-control-allow-origin: * ✓
  - Hotlink protection: گاهی اوقات با Referer=m.hqporner.com 404 می‌ده
    به‌خاطر همین استراتژی: بدون Referer یا با Referer=mydaddy.cc

  ─── کوکی ───
  - mydaddy.cc یه کوکی md_v_s=s ست می‌کنه (session marker) — لازم نیست
    برای CDN request ها پاس بدیم.

  ─── کیفیت‌ها ───
  - 360p, 720p, 1080p (هر سه همیشه موجود نیستن — باید از HTML استخراج بشن)
  - label توی source tag: <source src="..." title="360p" type="video/mp4">

استراتژی دانلود:
  1. fetch صفحه hqporner با aiohttp (سریع — بدون CF)
  2. fallback به curl_cffi با impersonate=chrome
  3. استخراج iframe URL (mydaddy.cc/video/{ID}/)
  4. fetch iframe با Referer=hqporner URL
  5. استخراج <source> URLs از iframe HTML
  6. multi-segment download با 32 workers (CDN سرعت بالایی داره)
  7. fallback به single-connection
  8. fallback به yt-dlp روی URL صفحه

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

logger = logging.getLogger("HqpornerHandler")

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
PROGRESS_INTERVAL = 1.0
CHUNK_SIZE = 1024 * 1024  # 1 MB (single connection)
MAX_RETRIES = 3
RETRY_DELAY = 2.0
MULTI_SEGMENT_MIN_SIZE = 5 * 1024 * 1024  # 5 MB

# ─── تنظیمات سرعت بالا ───────────────────────────────────────────────────────
# bigcdn.cc از Range پشتیبانی می‌کنه و سرعت بالایی داره
MULTI_SEGMENT_WORKERS = 32
MULTI_SEGMENT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB
CONNECTOR_LIMIT = 50
CONNECTOR_LIMIT_PER_HOST = 50

# دامنه‌های مجاز (hqporner + iframe host + CDN)
_ALLOWED_HOSTS = frozenset({
    "hqporner.com",
    "www.hqporner.com",
    "m.hqporner.com",
})

_ALLOWED_IFRAME_HOSTS = frozenset({
    "mydaddy.cc",
    "www.mydaddy.cc",
})

_ALLOWED_CDN_HOSTS = frozenset({
    "bigcdn.cc",
    "s28.bigcdn.cc",
    # ممکنه s29, s30 و... هم باشن — اجازه بدیم همه subdomain های bigcdn.cc
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_hqporner_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به hqporner هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".hqporner.com")
    except Exception:
        return False


def _is_cdn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به CDN مجاز هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_CDN_HOSTS or host.endswith(".bigcdn.cc")
    except Exception:
        return False


def _is_iframe_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به mydaddy.cc iframe هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_IFRAME_HOSTS
    except Exception:
        return False


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
    """تمیزکاری URL از escape های اضافی."""
    url = unquote(url).replace("&amp;", "&")
    url = url.replace("\\/", "/").replace("\\\\", "\\")
    # حذف trailing slash و quote characters
    url = re.sub(r'[\\/]+$', '', url)
    url = url.rstrip("',\"\\")
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
        title = re.sub(r"\s*[-|@]\s*(?:hqporner\.com|HQporner\.com)\s*$", "", title, flags=re.IGNORECASE)
        if title:
            return title

    # روش 2: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|@]\s*(?:hqporner\.com|HQporner\.com)\s*$", "", title, flags=re.IGNORECASE)
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
    """استخراج مدت زمان از meta description یا h1."""
    # از meta description: "Video duration is 41min 53sec."
    m = re.search(r'duration\s+is\s+(\d+)min(?:\s+(\d+)sec)?', html, re.IGNORECASE)
    if m:
        mins = int(m.group(1))
        secs = int(m.group(2)) if m.group(2) else 0
        return mins * 60 + secs

    # از <i class="fa fa-clock-o">41m 53s</i>
    m = re.search(r'fa-clock-o[^>]*>\s*</i>\s*<span[^>]*>\s*(\d+)m\s+(\d+)s', html, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    # ISO 8601 (PT41M53S)
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

    return None


def _extract_iframe_url(html: str) -> str:
    """
    استخراج URL اِ iframe mydaddy.cc از صفحه hqporner.

    ممکنه در این مکان‌ها باشه:
    1. <iframe src="//mydaddy.cc/video/{ID}/">
    2. altPlayer()/nativePlayer() JS: '/blocks/altplayer.php?i=//mydaddy.cc/video/{ID}/'
    """
    # روش 1: iframe src مستقیم
    iframe_pattern = re.compile(
        r'<iframe[^>]+src=["\'](?:https?:)?(//mydaddy\.cc/video/[a-zA-Z0-9]+/?[^"\']*)["\']',
        re.IGNORECASE,
    )
    for m in iframe_pattern.finditer(html):
        url = m.group(1).strip()
        # تبدیل به https://
        return "https:" + url if url.startswith("//") else url

    # روش 2: از JS altplayer parameter
    alt_pattern = re.compile(
        r"['\"](?:https?:)?(//mydaddy\.cc/video/[a-zA-Z0-9]+/?)['\"]",
        re.IGNORECASE,
    )
    for m in alt_pattern.finditer(html):
        url = m.group(1).strip()
        return "https:" + url if url.startswith("//") else url

    # روش 3: any iframe with /video/ path
    general_iframe = re.compile(
        r'<iframe[^>]+src=["\'](?:https?:)?(//[a-zA-Z0-9.-]+/video/[a-zA-Z0-9]+/?[^"\']*)["\']',
        re.IGNORECASE,
    )
    for m in general_iframe.finditer(html):
        url = m.group(1).strip()
        # exclude ad iframes
        if any(kw in url.lower() for kw in ["adtng", "goaserv", "mnaspm", "mayzaent", "zline", "smartpop"]):
            continue
        return "https:" + url if url.startswith("//") else url

    return ""


def _extract_video_sources_from_iframe(html: str) -> List[dict]:
    """
    استخراج URL های ویدیو از HTML iframe (mydaddy.cc).

    ساختار:
      <video id="flvv" poster="//s28.bigcdn.cc/pubs/{HASH}/main.jpg" ...>
        <source src="//s28.bigcdn.cc/pubs/{HASH}/360.mp4" title="360p" type="video/mp4" />
        <source src="//s28.bigcdn.cc/pubs/{HASH}/720.mp4" title="720p" type="video/mp4" />
        <source src="//s28.bigcdn.cc/pubs/{HASH}/1080.mp4" title="1080p" type="video/mp4" />
      </video>

    همچنین اگر adblock داشته باشیم، ممکنه فقط 360p توی HTML باشه.

    Returns:
        list of dicts: [{label, url, height, quality_key, method, is_hd}, ...]
    """
    sources = []
    seen_urls = set()

    # روش 1: استخراج از <source> tag
    source_pattern = re.compile(
        r'<source\b([^>]*)>',
        re.IGNORECASE,
    )
    for m in source_pattern.finditer(html):
        attrs_str = m.group(1)
        attrs = dict(re.findall(r'(\w[\w-]*)=["\']([^"\']*)["\']', attrs_str, re.IGNORECASE))
        src = attrs.get("src", "").strip()
        if not src:
            continue
        src = src.replace("\\/", "/").replace("&amp;", "&")
        # پروتکل نسبی
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://mydaddy.cc" + src

        # فقط CDN مجاز
        if not _is_cdn_url(src):
            continue

        # کیفیت از title attribute یا filename
        title_attr = attrs.get("title", "").strip()
        quality_text = ""
        height = 0
        is_hd = False

        if title_attr and re.search(r'\d{3,4}p', title_attr, re.IGNORECASE):
            m_q = re.search(r'(\d{3,4})p', title_attr, re.IGNORECASE)
            quality_text = f"{m_q.group(1)}p"
            height = int(m_q.group(1))
            is_hd = height >= 720
        else:
            # از filename
            m_f = re.search(r'/(\d{3,4})\.mp4', src, re.IGNORECASE)
            if m_f:
                quality_text = f"{m_f.group(1)}p"
                height = int(m_f.group(1))
                is_hd = height >= 720

        if not quality_text:
            continue

        if src in seen_urls:
            continue
        seen_urls.add(src)

        label = f"📺 MP4 {quality_text}"
        sources.append({
            "label": label,
            "url": src,
            "height": height,
            "quality_key": quality_text.lower(),
            "method": "source_tag",
            "is_hd": is_hd,
        })
        logger.info("Found source: %s (%s)", quality_text, src[:100])

    # روش 2: regex fallback برای پیدا کردن هر URL با pattern bigcdn.cc
    cdn_pattern = re.compile(
        r'(?:https?:)?//[a-zA-Z0-9.-]*bigcdn\.cc/[^\s"\'<>\)\]\\]+?/(\d{3,4})\.mp4',
        re.IGNORECASE,
    )
    for m in cdn_pattern.finditer(html):
        url = m.group(0).replace("\\/", "/")
        if url.startswith("//"):
            url = "https:" + url
        quality_text = f"{m.group(1)}p"
        height = int(m.group(1))

        if url in seen_urls:
            continue
        seen_urls.add(url)

        sources.append({
            "label": f"📺 MP4 {quality_text}",
            "url": url,
            "height": height,
            "quality_key": quality_text.lower(),
            "method": "cdn_regex",
            "is_hd": height >= 720,
        })
        logger.info("Found via CDN regex: %s (%s)", quality_text, url[:100])

    # مرتب‌سازی: بالاترین کیفیت اول
    sources.sort(key=lambda q: q.get("height", 0), reverse=True)
    return sources


def _extract_thumbnail_from_iframe(html: str) -> str:
    """استخراج poster از تگ video در iframe."""
    m = re.search(r'<video[^>]+poster=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        url = m.group(1).replace("\\/", "/")
        if url.startswith("//"):
            url = "https:" + url
        return url
    return ""


# ─── Fetch Pages ───────────────────────────────────────────────────────────


async def _fetch_page(url, jar=None, referer=None, method="aiohttp"):
    """
    fetch صفحه با fallback.
    """
    headers = dict(_DEFAULT_HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "cross-site"

    # روش 1: aiohttp
    if method in ("aiohttp", "all"):
        try:
            local_jar = jar or CookieJar(unsafe=True)
            timeout = ClientTimeout(total=30, connect=10)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookie_jar=local_jar) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="replace")
                        logger.info("Page fetched via aiohttp: %s (size=%d, status=%d)", url[:80], len(html), resp.status)
                        return html, local_jar, ""
                    logger.warning("aiohttp fetch: HTTP %d for %s", resp.status, url[:80])
        except Exception as e:
            logger.warning(f"aiohttp fetch error: {e}")

    # روش 2: curl_cffi (fallback)
    if method in ("curl_cffi", "all") and _check_curl_cffi():
        try:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession() as session:
                resp = await session.get(
                    url, impersonate="chrome",
                    headers=headers, allow_redirects=True, timeout=30,
                )
                if resp.status_code == 200 and resp.text:
                    text = resp.text
                    logger.info("Page fetched via curl_cffi: %s (size=%d)", url[:80], len(text))
                    local_jar = CookieJar(unsafe=True)
                    try:
                        for cookie in session.cookies.jar:
                            try:
                                local_jar.update_cookies({cookie.name: cookie.value})
                            except Exception:
                                pass
                    except Exception:
                        pass
                    return text, local_jar, ""
                logger.warning("curl_cffi fetch: HTTP %d", resp.status_code)
        except Exception as e:
            logger.warning(f"curl_cffi fetch error: {e}")

    return None, jar, "Failed to fetch page"


# ─── Main API: extract qualities ──────────────────────────────────────────


async def extract_hqporner_qualities(url, progress_cb=None):
    """
    استخراج کیفیت‌های ویدیو از URL صفحه hqporner.

    Returns:
        (sources, title, info_dict)
        sources: [{label, url, height, quality_key, method, is_hd}, ...]
        title: str
        info_dict: {thumbnail, page_url, cookies, duration, fetch_method, iframe_url}
    """
    if not is_hqporner_url(url):
        return [], "Invalid URL — host not allowed", {}

    if progress_cb:
        await progress_cb("🔄 **دریافت اطلاعات صفحه...**")

    # 1. fetch صفحه hqporner
    jar = CookieJar(unsafe=True)
    html, jar, error = await _fetch_page(url, jar, method="all")

    if not html:
        logger.error("Failed to fetch hqporner page: %s", error)
        return [], f"خطا در دریافت صفحه: {error}", {}

    title = _extract_title(html)
    thumbnail = _extract_thumbnail(html)
    duration = _extract_duration(html)

    if progress_cb:
        await progress_cb("🔎 **جستجوی پلیر ویدیو...**")

    # 2. استخراج iframe URL
    iframe_url = _extract_iframe_url(html)
    if not iframe_url:
        logger.error("No mydaddy.cc iframe found in page")
        return [], "پلیر ویدیو (iframe) در صفحه پیدا نشد", {}

    logger.info("Found iframe URL: %s", iframe_url[:100])

    # 3. fetch iframe page
    iframe_html, iframe_jar, iframe_error = await _fetch_page(
        iframe_url, jar=None, referer=url, method="all",
    )
    if not iframe_html:
        logger.error("Failed to fetch iframe: %s", iframe_error)
        return [], f"خطا در دریافت iframe پلیر: {iframe_error}", {}

    # 4. استخراج source URLs از iframe HTML
    sources = _extract_video_sources_from_iframe(iframe_html)

    if not sources:
        logger.error("No video sources found in iframe HTML")
        return [], "URL ویدیو در iframe پیدا نشد", {}

    # poster از iframe (بهتر از og:image)
    iframe_poster = _extract_thumbnail_from_iframe(iframe_html)
    if iframe_poster:
        thumbnail = iframe_poster

    # جمع‌آوری کوکی‌ها
    cookies = {}
    if iframe_jar:
        for cookie in iframe_jar:
            cookies[cookie.key] = cookie.value

    logger.info("Found %d video sources", len(sources))

    if progress_cb:
        labels = ", ".join(s["label"] for s in sources)
        dur_str = ""
        if duration:
            mins, secs = divmod(duration, 60)
            dur_str = f" ({mins}:{secs:02d})"
        await progress_cb(
            f"✅ **پیدا شد:** {title[:50]}{dur_str}\n"
            f"🎞 کیفیت‌ها: {labels}"
        )

    return sources, title, {
        "thumbnail": thumbnail,
        "page_url": url,
        "iframe_url": iframe_url,
        "cookies": cookies,
        "duration": duration,
        "fetch_method": "aiohttp",
        "iframe_html_size": len(iframe_html),
    }


# ─── Download: Multi-segment (fast) ───────────────────────────────────────


active_downloads: dict = {}


async def _download_multi_segment(
    direct_url, filepath, referer, cookies, progress_cb, dl_id="",
    num_workers=MULTI_SEGMENT_WORKERS,
):
    """
    دانلود چند تیکه‌ای با work-queue pattern — OPTIMIZED برای سرعت بالا.

    bigcdn.cc از Range پشتیبانی می‌کنه و سرعت بالایی داره، پس از 32 workers
    و 10MB chunks استفاده می‌کنیم.

    نکته مهم: به‌خاطر رفتار inconsistent CDN با Referer، درخواست‌های CDN رو
    بدون Referer می‌فرستیم (بهترین نتیجه رو داشته).
    """
    try:
        # هدر برای CDN — بدون Referer (بهترین نتیجه طبق تست)
        # Sec-Fetch-Site = none مهمه تا شبیه درخواست مستقیم بشیم
        cdn_headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",  # برای دریافت Content-Length واقعی
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        }
        # اگه Referer داده شده mydaddy.cc باشه، استفاده می‌کنیم (گاهی کار می‌کنه)
        # اگه hqporner باشه، حذف می‌کنیم (hotlink protection)
        if referer and "mydaddy.cc" in referer:
            cdn_headers["Referer"] = referer

        content_length = 0
        accept_ranges = ""

        # ─── 1. HEAD request ───
        try:
            timeout = ClientTimeout(total=10, connect=5)
            async with aiohttp.ClientSession(timeout=timeout, headers=cdn_headers, cookies=cookies) as s:
                async with s.head(direct_url, allow_redirects=True) as r:
                    if r.status in (200, 206):
                        content_length = int(r.headers.get("Content-Length", 0))
                        accept_ranges = r.headers.get("Accept-Ranges", "").lower()
                        ct = r.headers.get("Content-Type", "")
                        if ct and not ct.startswith("video/"):
                            logger.warning("HEAD returned non-video content-type: %s", ct)
                    elif r.status == 404:
                        # fallback: بدون referer امتحان کن
                        logger.warning("HEAD 404 with referer, retrying without referer")
                        cdn_headers.pop("Referer", None)
                        async with aiohttp.ClientSession(timeout=timeout, headers=cdn_headers, cookies=cookies) as s2:
                            async with s2.head(direct_url, allow_redirects=True) as r2:
                                if r2.status in (200, 206):
                                    content_length = int(r2.headers.get("Content-Length", 0))
                                    accept_ranges = r2.headers.get("Accept-Ranges", "").lower()
                    elif r.status == 403:
                        return False, "HTTP_403", 0
        except Exception as e:
            logger.warning(f"HEAD request failed: {e}")

        # ─── 2. probe با Range اگه HEAD کار نکرد ───
        if content_length == 0:
            try:
                timeout = ClientTimeout(total=10, connect=5)
                probe_headers = dict(cdn_headers)
                probe_headers["Range"] = "bytes=0-0"
                async with aiohttp.ClientSession(timeout=timeout, headers=probe_headers, cookies=cookies) as s:
                    async with s.get(direct_url, allow_redirects=True) as r:
                        if r.status in (200, 206):
                            if r.status == 206:
                                accept_ranges = "bytes"
                                cr = r.headers.get("Content-Range", "")
                                m = re.search(r"/(\d+)", cr)
                                if m:
                                    content_length = int(m.group(1))
                            else:
                                content_length = int(r.headers.get("Content-Length", 0))
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
            f"📥 **Downloading...**\n"
            f"💾 Size: {total_mb:.1f} MB\n"
            f"🔥 {num_workers} parallel workers"
        )

        # ─── 3. آماده‌سازی chunks ───
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
        logger.info(
            "[DL-HQP] Work-queue: %d chunks, %d workers, total=%d",
            total_chunks, num_workers, content_length,
        )

        # pre-allocate file با aiofiles (non-blocking)
        try:
            async with aiofiles.open(filepath, "wb") as f:
                await f.truncate(content_length)
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
        connector = TCPConnector(
            limit=CONNECTOR_LIMIT,
            limit_per_host=CONNECTOR_LIMIT_PER_HOST,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
        )
        shared_session = aiohttp.ClientSession(
            timeout=shared_timeout, headers=cdn_headers,
            cookies=cookies, connector=connector,
        )
        # file handle باز نگه می‌داریم برای پرفورمنس
        shared_file = await aiofiles.open(filepath, "r+b")

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
                worker_max_retries = MAX_RETRIES

                for attempt in range(worker_max_retries):
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
                            chunk_data = bytearray()
                            async for piece in resp.content.iter_chunked(CHUNK_SIZE):
                                if not piece:
                                    continue
                                if active_downloads.get(dl_id, {}).get("cancelled"):
                                    return False
                                chunk_data.extend(piece)
                            if len(chunk_data) != chunk_size:
                                raise Exception(
                                    f"Size mismatch: expected {chunk_size}, got {len(chunk_data)}"
                                )
                            async with file_write_lock:
                                await shared_file.seek(byte_start)
                                await shared_file.write(bytes(chunk_data))
                            downloaded_bytes[c_idx] = chunk_size
                            async with progress_lock:
                                completed_chunks[0] += 1
                                await _update_progress()
                            break
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(
                            "[DL-HQP] W%d c%d attempt %d failed: %s",
                            worker_id, c_idx, attempt + 1, e,
                        )
                        if attempt < worker_max_retries - 1:
                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
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
                await shared_file.close()
            except Exception:
                pass
            try:
                await shared_session.close()
            except Exception:
                pass

            if active_downloads.get(dl_id, {}).get("cancelled"):
                _cleanup_file(filepath)
                return False, "Cancelled by user", 0

            # تشخیص failure های واقعی (False یا Exception)
            real_failures = []
            for i, r in enumerate(results):
                if r is True:
                    continue
                if isinstance(r, Exception):
                    real_failures.append(f"worker{i}: {r}")
                elif r is False:
                    real_failures.append(f"worker{i}: returned False")
            if real_failures or failed_chunks:
                logger.warning(
                    "[DL-HQP] %d worker failures, %d chunk failures",
                    len(real_failures), len(failed_chunks),
                )
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(real_failures)+len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-HQP] Work-queue error: {e}", exc_info=True)
            try:
                await shared_file.close()
            except Exception:
                pass
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
        logger.info(
            "[DL-HQP] Multi-segment DONE | size=%s | time=%.1fs | avg_speed=%.1f MB/s",
            _format_size(file_size), elapsed, avg_speed,
        )
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-HQP] Multi-segment error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Download: Single connection (fallback) ───────────────────────────────


async def _download_single_aiohttp(url, filepath, referer, cookies, progress_cb, dl_id=""):
    """دانلود با connection واحد (fallback)."""
    # CDN هدر — بدون Referer (طبق تست بهترین نتیجه)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if referer and "mydaddy.cc" in referer:
        headers["Referer"] = referer

    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookies=cookies) as s:
                async with s.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        # retry without referer
                        if "Referer" in headers and resp.status in (403, 404):
                            logger.info("[DL-HQP] Single 403/404 with referer, retry without")
                            headers.pop("Referer", None)
                            continue
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
            "--add-header", "Referer:https://mydaddy.cc/",
            "-o", filepath,
        ]
        if has_curl_cffi:
            cmd.extend(["--impersonate", "chrome"])
        cmd.append(url)
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
        logger.info(f"[DL-HQP] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-HQP] yt-dlp error: {e}", exc_info=True)
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
    return (
        f"📥 **Downloading...**\n`[{bar}]`\n"
        f"💾 {total}  •  ⚡ {speed}\n"
        f"📊 {pct}%  •  ⏱ ETA: {eta}"
    )


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


async def download_hqporner_video(
    page_url, video_url, filepath, progress_cb=None, cookies=None, dl_id="",
    quality_key="",
):
    """دانلود ویدیو از hqporner با کیفیت انتخاب شده."""
    if not is_hqporner_url(page_url):
        return False, "URL host not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0
    if progress_cb is None:
        async def _noop(msg): pass
        progress_cb = _noop
    if dl_id and dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}
    referer = "https://mydaddy.cc/"
    if not cookies:
        cookies = {}

    # ── روش 1: multi-segment ──
    logger.info(f"[DL-HQP] Attempt 1: multi-segment ({MULTI_SEGMENT_WORKERS} workers)")
    success, error, size = await _download_multi_segment(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0
    if error == "HTTP_403":
        logger.info("[DL-HQP] 403, refreshing session...")
        if progress_cb:
            await progress_cb("🔄 **Refreshing session...**")
        # refresh: دوباره iframe fetch کن
        try:
            new_sources, _, new_info = await extract_hqporner_qualities(page_url, progress_cb=None)
            if new_sources:
                # پیدا کردن همون کیفیت
                new_video_url = None
                for q in new_sources:
                    if q.get("quality_key") == quality_key:
                        new_video_url = q["url"]
                        break
                if not new_video_url:
                    new_video_url = new_sources[0]["url"]
                video_url = new_video_url
                new_cookies = new_info.get("cookies", {})
                cookies.update(new_cookies)
                logger.info("[DL-HQP] Got fresh URL")
        except Exception as e:
            logger.warning(f"[DL-HQP] refresh failed: {e}")
        success, error, size = await _download_multi_segment(
            video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
        )
        if success:
            return True, "", size
    logger.info(f"[DL-HQP] Multi-segment failed: {error}")
    _cleanup_file(filepath)

    # ── روش 2: single-connection ──
    logger.info("[DL-HQP] Attempt 2: single-connection")
    success, error, size = await _download_single_aiohttp(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    logger.info(f"[DL-HQP] Single failed: {error}")
    _cleanup_file(filepath)

    # ── روش 3: yt-dlp ──
    logger.info("[DL-HQP] Attempt 3: yt-dlp on page URL")
    success, error, size = await _download_with_ytdlp(
        page_url, filepath, progress_cb, quality_key=quality_key,
    )
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


async def download_hqporner_direct(
    url, filepath, progress_cb=None, video_url="", quality="high", dl_id="",
):
    """Wrapper برای سازگاری با bot architecture."""
    if not video_url:
        qualities, title, info = await extract_hqporner_qualities(url, progress_cb)
        if not qualities:
            return False, title or "Extraction failed", 0
        selected = None
        # تطبیق کیفیت
        for q in qualities:
            if q.get("quality_key") == quality:
                selected = q
                break
        if not selected:
            if quality in ("high", "best", "1080p"):
                # اولین کیفیت HD
                hd = [q for q in qualities if q.get("is_hd")]
                selected = hd[0] if hd else qualities[0]
            elif quality in ("low", "worst", "360p"):
                selected = qualities[-1]
            elif quality in ("medium", "720p"):
                # 720p اگه باشه، وگرنه کیفیت وسط
                for q in qualities:
                    if q.get("quality_key") == "720p":
                        selected = q
                        break
                if not selected:
                    selected = qualities[len(qualities) // 2] if len(qualities) > 1 else qualities[0]
            else:
                selected = qualities[0]
        video_url = selected["url"]
        quality_key = selected.get("quality_key", "")
        cookies = info.get("cookies", {})
    else:
        qualities, title, info = await extract_hqporner_qualities(url, progress_cb)
        cookies = info.get("cookies", {}) if info else {}
        quality_key = quality

    return await download_hqporner_video(
        url, video_url, filepath, progress_cb,
        cookies=cookies, dl_id=dl_id, quality_key=quality_key,
    )


# ─── Self-test ─────────────────────────────────────────────────────────────


async def _self_test():
    """تست خودي هندلر."""
    test_url = "https://m.hqporner.com/hdporn/123584-uhm---_youre_a_little_close.html"
    print(f"\n{'═' * 80}")
    print(f"Self-test: {test_url}")
    print(f"{'═' * 80}\n")

    async def progress(msg):
        print(f"  → {msg}")

    sources, title, info = await extract_hqporner_qualities(test_url, progress_cb=progress)

    print(f"\n  Title: {title}")
    print(f"  Thumbnail: {info.get('thumbnail', '')[:120]}")
    print(f"  Duration: {info.get('duration', '?')}s")
    print(f"  iframe URL: {info.get('iframe_url', '')[:120]}")
    print(f"  iframe HTML size: {info.get('iframe_html_size', '?')}")
    print(f"  Cookies: {list(info.get('cookies', {}).keys())}")
    print(f"\n  Sources ({len(sources)}):")
    for s in sources:
        print(f"    [{s['quality_key']:6s}] {s['url'][:120]} ({s['method']})")

    return sources, title, info


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_self_test())
