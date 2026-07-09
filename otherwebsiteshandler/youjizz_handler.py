"""
youjizz_handler.py
──────────────────
استخراج و دانلود ویدیو از youjizz.com

روش کار (بر اساس تحلیل واقعی صفحه):

  ─── ساختار صفحه ───
  صفحه ویدیو HTML شامل:
    1. یه inline <script> که یه متغیر dataEncodings تعریف می‌کنه به‌صورت JSON
       array، شامل تمام کیفیت‌های موجود (MP4 + HLS).
    2. یه تگ <video id="yj-fluid"> که با Fluid Player مقداردهی می‌شه.
    3. JS یک subset از dataEncodings رو بر اساس قابلیت‌های browser (HLS, adblock,
       iOS و...) انتخاب می‌کنه و به‌عنوان <source> به video tag اضافه می‌کنه.

  ─── ساختار dataEncodings ───
  هر entry:
    {
      "quality": "240",          ← ارتفاع به‌صورت string
      "filename": "//cdne-mobile.youjizz.com/videos/...mp4?validfrom=...&validto=...&rate=...&hash=...",
      "is_old_origin": "0",
      "version": "2",
      "name": "240p"             ← نمایش
    }

  دو نوع URL داریم:
    - MP4:  //cdne-mobile.youjizz.com/videos/{path}/{HASH}-{W}-{H}-{KBPS}-h264.mp4?...
    - HLS:  //abre-videos.youjizz.com/_hls/videos/.../master.m3u8?... (یا hlse-videos.youjizz.com)

  ما فقط MP4 رو استفاده می‌کنیم (ساده‌تر و سریع‌تر).

  ─── سرور ───
  - www.youjizz.com: nginx (بدون Cloudflare!)
  - cdne-mobile.youjizz.com: CDN (بدون Cloudflare!)
  - URLs امضا‌دار با TTL کوتاه (validfrom/validto) — باید هر بار fetch بشن

  ─── رفتار CDN ───
  - Accept-Ranges: bytes ✓ (multi-segment download کار می‌کنه)
  - بدون hotlink protection (همه referer ها کار می‌کنن — حتی بدون referer)
  - Content-Type: video/mp4
  - Cache-Control: max-age=10+ days (cache قوی)

  ─── کوکی ───
  - sessionId, commentPhrase, RNLBSERVERID — برای session persistence لازم نیست
    برای CDN request ها (CDN URL ها signed هستن).

کیفیت‌ها (همیشه موجود نیستن — از HTML استخراج می‌شن):
  - 240p, 288p, 360p, 480p, 720p, 1080p + Auto (HLS)

استراتژی دانلود:
  1. fetch صفحه youjizz با aiohttp (سریع — بدون CF)
  2. fallback به curl_cffi با impersonate=chrome
  3. استخراج dataEncodings JSON از HTML
  4. فیلتر فقط MP4 URLs (نه HLS)
  5. multi-segment download با 32 workers
  6. fallback به single-connection
  7. fallback به yt-dlp روی URL صفحه

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

logger = logging.getLogger("YoujizzHandler")

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
MULTI_SEGMENT_WORKERS = 32
MULTI_SEGMENT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB
CONNECTOR_LIMIT = 50
CONNECTOR_LIMIT_PER_HOST = 50

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset({
    "youjizz.com",
    "www.youjizz.com",
    "m.youjizz.com",
})

# CDN مجاز (برای ویدیو)
_ALLOWED_CDN_HOSTS = frozenset({
    "cdne-mobile.youjizz.com",
    "cdne-pics.youjizz.com",  # برای thumbnails
    "abre-videos.youjizz.com",  # HLS (fallback)
    "hlse-videos.youjizz.com",  # HLS (fallback)
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_youjizz_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به youjizz هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".youjizz.com")
    except Exception:
        return False


def _is_cdn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به CDN مجاز هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_CDN_HOSTS or host.endswith(".youjizz.com")
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
    url = url.replace("\\/", "/").replace("\\\\", "\\")
    url = unquote(url).replace("&amp;", "&")
    # اگر URL دوبار decode شده، دوبار encode نشده باشه
    # نکته: hash در URL باید percent-encoded بمونه — اما وقتی HTML رو parse می‌کنیم
    # &amp; به & تبدیل می‌شه و %2F باید بمونه
    # اما برای امنیت more، فقط &amp; رو fix می‌کنیم
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
        title = re.sub(r"\s*[-|@]\s*(?:youjizz\.com|YouJizz)\s*$", "", title, flags=re.IGNORECASE)
        if title:
            return title

    # روش 2: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|@]\s*(?:youjizz\.com|YouJizz)\s*$", "", title, flags=re.IGNORECASE)
        return title or "Untitled"

    return "Untitled"


def _extract_thumbnail(html: str) -> str:
    m = re.search(
        r'(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        url = m.group(1).strip()
        if url.startswith("//"):
            url = "https:" + url
        return url
    return ""


def _extract_duration(html: str) -> Optional[int]:
    """استخراج مدت زمان از og:video:duration."""
    # از meta tag
    m = re.search(
        r'(?:property|name)=["\']og:video:duration["\']\s+content=["\']?(\d+)["\']?',
        html, re.IGNORECASE,
    )
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # از JSON-LD
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


def _extract_data_encodings(html: str) -> List[dict]:
    """
    استخراج dataEncodings JSON از HTML.

    ساختار:
      var dataEncodings = [...];

    نکته: JSON ممکنه شامل کاراکترهای escape شده مثل \\/ باشه.
    """
    # الگوی اصلی
    m = re.search(
        r'var\s+dataEncodings\s*=\s*(\[.*?\]);',
        html, re.DOTALL,
    )
    if not m:
        # الگوی alt با \n بین } و ]
        m = re.search(
            r'var\s+dataEncodings\s*=\s*(\[[\s\S]*?\])\s*;',
            html,
        )
        if not m:
            logger.error("dataEncodings پیدا نشد در HTML")
            return []

    raw_json = m.group(1)
    # fix escaped slashes (JSON معتبر)
    # نکته: \\/ در JavaScript string، در JSON معتبر نیست — باید / بشه
    # اما &amp; رو نباید decode کنیم چون hash در URL هست

    try:
        # اول با JSON parser مستقیم امتحان کن (اگه \\/ نداره)
        data = json.loads(raw_json)
        logger.info("dataEncodings parsed directly: %d entries", len(data))
        return data
    except json.JSONDecodeError as e:
        logger.debug("Direct parse failed: %s, trying cleaned version", e)
        # fix \\/ to /
        cleaned = raw_json.replace("\\/", "/")
        try:
            data = json.loads(cleaned)
            logger.info("dataEncodings parsed after \\/ fix: %d entries", len(data))
            return data
        except json.JSONDecodeError as e2:
            logger.error("dataEncodings parse failed even after cleaning: %s", e2)
            # تلاش آخر: regex fallback برای استخراج filename و quality
            return _extract_encodings_regex(raw_json)


def _extract_encodings_regex(raw: str) -> List[dict]:
    """Fallback: استخراج با regex اگه JSON parse شکست خورد."""
    encodings = []
    # الگو: {"quality":"360","filename":"...","name":"360p"}
    pattern = re.compile(
        r'\{\s*"quality"\s*:\s*"([^"]+)"\s*,\s*"filename"\s*:\s*"([^"]+)"\s*,\s*(?:"is_old_origin"\s*:\s*"[^"]*"\s*,\s*)?"version"\s*:\s*"([^"]*)"\s*,\s*"name"\s*:\s*"([^"]+)"\s*\}',
        re.DOTALL,
    )
    for m in pattern.finditer(raw):
        quality, filename, version, name = m.group(1), m.group(2), m.group(3), m.group(4)
        encodings.append({
            "quality": quality,
            "filename": filename.replace("\\/", "/"),
            "version": version,
            "name": name,
        })
    logger.info("Regex fallback extracted %d encodings", len(encodings))
    return encodings


def _extract_video_sources(html: str) -> List[dict]:
    """
    استخراج URL های ویدیو از HTML با فیلتر فقط MP4.

    Returns:
        list of dicts: [{label, url, height, quality_key, method, is_hd}, ...]
    """
    sources = []
    seen_urls = set()

    encodings = _extract_data_encodings(html)
    if not encodings:
        logger.error("No dataEncodings found")
        return []

    # فقط MP4 (نه HLS)
    mp4_encodings = [e for e in encodings if "_hls" not in e.get("filename", "")]

    if not mp4_encodings:
        logger.warning("No MP4 encodings, only HLS available")
        return []

    # sort by quality descending
    def _q_key(e):
        try:
            return int(e.get("quality", "0"))
        except (ValueError, TypeError):
            return 0

    mp4_encodings.sort(key=_q_key, reverse=True)

    for e in mp4_encodings:
        filename = e.get("filename", "").strip()
        if not filename:
            continue

        # تبدیل protocol-relative
        if filename.startswith("//"):
            url = "https:" + filename
        elif filename.startswith("/"):
            url = "https://www.youjizz.com" + filename
        elif not filename.startswith("http"):
            continue
        else:
            url = filename

        # تمیزکاری
        url = _clean_url(url)

        # validation
        if not _is_cdn_url(url):
            logger.debug("Skipping non-CDN URL: %s", url[:80])
            continue

        if url in seen_urls:
            continue
        seen_urls.add(url)

        # کیفیت
        quality_str = str(e.get("quality", "")).strip()
        name_str = e.get("name", "").strip()
        try:
            height = int(quality_str)
        except (ValueError, TypeError):
            # اگه quality عدد نیست (مثلا "Auto")، skip
            continue

        quality_text = f"{height}p"
        is_hd = height >= 720
        label = f"📺 MP4 {quality_text}"

        sources.append({
            "label": label,
            "url": url,
            "height": height,
            "quality_key": quality_text.lower(),
            "method": "dataEncodings",
            "is_hd": is_hd,
        })
        logger.info("Found encoding: %s (%s)", quality_text, url[:100])

    return sources


# ─── Fetch Page ───────────────────────────────────────────────────────────


async def _fetch_page(url, jar=None, method="aiohttp"):
    """fetch صفحه با fallback."""
    headers = dict(_DEFAULT_HEADERS)

    if method in ("aiohttp", "all"):
        try:
            local_jar = jar or CookieJar(unsafe=True)
            timeout = ClientTimeout(total=30, connect=10)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookie_jar=local_jar) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="replace")
                        logger.info("Page fetched via aiohttp: %s (size=%d)", url[:80], len(html))
                        return html, local_jar, ""
                    logger.warning("aiohttp fetch: HTTP %d for %s", resp.status, url[:80])
        except Exception as e:
            logger.warning(f"aiohttp fetch error: {e}")

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


async def extract_youjizz_qualities(url, progress_cb=None):
    """
    استخراج کیفیت‌های ویدیو از URL صفحه youjizz.

    Returns:
        (sources, title, info_dict)
    """
    if not is_youjizz_url(url):
        return [], "Invalid URL — host not allowed", {}

    if progress_cb:
        await progress_cb("🔄 **دریافت اطلاعات صفحه...**")

    jar = CookieJar(unsafe=True)
    html, jar, error = await _fetch_page(url, jar, method="all")

    if not html:
        logger.error("Failed to fetch page: %s", error)
        return [], f"خطا در دریافت صفحه: {error}", {}

    title = _extract_title(html)
    thumbnail = _extract_thumbnail(html)
    duration = _extract_duration(html)

    if progress_cb:
        await progress_cb("🔎 **استخراج کیفیت‌های ویدیو...**")

    sources = _extract_video_sources(html)

    if not sources:
        logger.error("No video sources found in page")
        return [], "URL ویدیو در صفحه پیدا نشد", {}

    cookies = {}
    if jar:
        for cookie in jar:
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
        "cookies": cookies,
        "duration": duration,
        "fetch_method": "aiohttp",
        "html_size": len(html),
    }


# ─── Download: Multi-segment (fast) ───────────────────────────────────────


active_downloads: dict = {}


async def _download_multi_segment(
    direct_url, filepath, referer, cookies, progress_cb, dl_id="",
    num_workers=MULTI_SEGMENT_WORKERS,
):
    """
    دانلود چند تیکه‌ای با work-queue pattern — OPTIMIZED برای سرعت بالا.

    cdne-mobile.youjizz.com از Range پشتیبانی می‌کنه و سرعت بالایی داره.
    """
    try:
        cdn_headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        }
        # youjizz CDN بدون hotlink protection، پس Referer اختیاریه
        # اما برای امنیت more، Referer از youjizz.com می‌ذاریم
        if referer:
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
                    elif r.status == 403:
                        return False, "HTTP_403", 0
                    elif r.status == 470:
                        # account expired — URL منقضی شده
                        return False, "URL_EXPIRED", 0
        except Exception as e:
            logger.warning(f"HEAD request failed: {e}")

        # ─── 2. probe با Range ───
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
                        elif r.status == 470:
                            return False, "URL_EXPIRED", 0
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
            "[DL-YJ] Work-queue: %d chunks, %d workers, total=%d",
            total_chunks, num_workers, content_length,
        )

        # pre-allocate file
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
                            "[DL-YJ] W%d c%d attempt %d failed: %s",
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
                    "[DL-YJ] %d worker failures, %d chunk failures",
                    len(real_failures), len(failed_chunks),
                )
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(real_failures)+len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-YJ] Work-queue error: {e}", exc_info=True)
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
            "[DL-YJ] Multi-segment DONE | size=%s | time=%.1fs | avg_speed=%.1f MB/s",
            _format_size(file_size), elapsed, avg_speed,
        )
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-YJ] Multi-segment error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Download: Single connection (fallback) ───────────────────────────────


async def _download_single_aiohttp(url, filepath, referer, cookies, progress_cb, dl_id=""):
    """دانلود با connection واحد (fallback)."""
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if referer:
        headers["Referer"] = referer

    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookies=cookies) as s:
                async with s.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        if resp.status == 470:
                            error = "URL_EXPIRED"
                            break
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
    if quality_key in ("720p", "480p", "1080p", "360p", "240p"):
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
            "--add-header", "Referer:https://www.youjizz.com/",
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
        logger.info(f"[DL-YJ] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-YJ] yt-dlp error: {e}", exc_info=True)
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


async def download_youjizz_video(
    page_url, video_url, filepath, progress_cb=None, cookies=None, dl_id="",
    quality_key="",
):
    """دانلود ویدیو از youjizz با کیفیت انتخاب شده."""
    if not is_youjizz_url(page_url):
        return False, "URL host not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0
    if progress_cb is None:
        async def _noop(msg): pass
        progress_cb = _noop
    if dl_id and dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}
    referer = "https://www.youjizz.com/"
    if not cookies:
        cookies = {}

    # ── روش 1: multi-segment ──
    logger.info(f"[DL-YJ] Attempt 1: multi-segment ({MULTI_SEGMENT_WORKERS} workers)")
    success, error, size = await _download_multi_segment(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0

    # اگه URL منقضی شده یا 403، یه URL تازه بگیر
    if error in ("HTTP_403", "URL_EXPIRED"):
        logger.info(f"[DL-YJ] {error}, refreshing session...")
        if progress_cb:
            await progress_cb("🔄 **Refreshing session...**")
        try:
            new_sources, _, new_info = await extract_youjizz_qualities(page_url, progress_cb=None)
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
                logger.info("[DL-YJ] Got fresh URL")
        except Exception as e:
            logger.warning(f"[DL-YJ] refresh failed: {e}")

        # retry multi-segment با URL تازه
        success, error, size = await _download_multi_segment(
            video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
        )
        if success:
            return True, "", size
    logger.info(f"[DL-YJ] Multi-segment failed: {error}")
    _cleanup_file(filepath)

    # ── روش 2: single-connection ──
    logger.info("[DL-YJ] Attempt 2: single-connection")
    success, error, size = await _download_single_aiohttp(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    logger.info(f"[DL-YJ] Single failed: {error}")
    _cleanup_file(filepath)

    # ─ـ روش 3: yt-dlp ──
    logger.info("[DL-YJ] Attempt 3: yt-dlp on page URL")
    success, error, size = await _download_with_ytdlp(
        page_url, filepath, progress_cb, quality_key=quality_key,
    )
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


async def download_youjizz_direct(
    url, filepath, progress_cb=None, video_url="", quality="high", dl_id="",
):
    """Wrapper برای سازگاری با bot architecture."""
    if not video_url:
        qualities, title, info = await extract_youjizz_qualities(url, progress_cb)
        if not qualities:
            return False, title or "Extraction failed", 0
        selected = None
        for q in qualities:
            if q.get("quality_key") == quality:
                selected = q
                break
        if not selected:
            if quality in ("high", "best", "1080p"):
                hd = [q for q in qualities if q.get("is_hd")]
                selected = hd[0] if hd else qualities[0]
            elif quality in ("low", "worst", "240p"):
                selected = qualities[-1]
            elif quality in ("medium", "720p", "360p", "480p"):
                for q in qualities:
                    if q.get("quality_key") == quality:
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
        qualities, title, info = await extract_youjizz_qualities(url, progress_cb)
        cookies = info.get("cookies", {}) if info else {}
        quality_key = quality

    return await download_youjizz_video(
        url, video_url, filepath, progress_cb,
        cookies=cookies, dl_id=dl_id, quality_key=quality_key,
    )


# ─── Self-test ─────────────────────────────────────────────────────────────


async def _self_test():
    """تست خودي هندلر."""
    test_url = "https://www.youjizz.com/videos/i-fucked-my-18-yo--sleeping-step-sister-and-cum-in-her-pussy.-oliver-strelly-57123671.html"
    print(f"\n{'═' * 80}")
    print(f"Self-test: {test_url}")
    print(f"{'═' * 80}\n")

    async def progress(msg):
        print(f"  → {msg}")

    sources, title, info = await extract_youjizz_qualities(test_url, progress_cb=progress)

    print(f"\n  Title: {title}")
    print(f"  Thumbnail: {info.get('thumbnail', '')[:120]}")
    print(f"  Duration: {info.get('duration', '?')}s")
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
