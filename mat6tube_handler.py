"""
mat6tube_handler.py
───────────────────
استخراج و دانلود ویدیو از mat6tube.com

روش کار (بر اساس تحلیل واقعی صفحه):

  ─── ساختار صفحه ───
  صفحه ویدیو HTML شامل:
    1. یه inline <script> که window.playlist رو به‌صورت JSON تعریف می‌کنه:

        window.playlist = {
          "image": "https://cdn2.pvvstream.pro/videos/{ID}/{VID}/preview_800.jpg",
          "sources": [
            {"file": "https://cdn2.pvvstream.pro/videos/{ID}/{VID}/vid_720p.mp4?secure=...", "label":"720", "type":"mp4"},
            {"file": "https://cdn2.pvvstream.pro/videos/{ID}/{VID}/vid_480p.mp4?secure=...", "label":"480", "type":"mp4","default":true},
            {"file": "https://cdn.pvvstream.pro/videos/{ID}/{VID}/vid_360p.mp4?rs=...&rb=...&secure=...", "label":"360", "type":"mp4"},
            {"file": "https://cdn2.pvvstream.pro/videos/{ID}/{VID}/vid_240p.mp4?secure=...", "label":"240", "type":"mp4"}
          ],
          "trusted": true,
          "tracks": [...],
          "cdn": 1
        };

    2. Player: JW Player 8.34.5
    3. window.v_id = "-185044521_456239719"  (video id)
    4. window.downloadUrl = "/download/-185044521_456239719?h=...&e=..."

  ─── ساختار URL ویدیو ───
  https://cdn2.pvvstream.pro/videos/{OWNER_ID}/{VIDEO_ID}/vid_{QUALITY}p.mp4?secure={TIMESTAMP}-{HMAC}
    - OWNER_ID: عدد منفی (مثل -185044521)
    - VIDEO_ID: عدد مثبت (مثل 456239719)
    - QUALITY: 240, 360, 480, 720 (و شاید 1080)
    - secure: HMAC امضا‌دار با TTL کوتاه

  نکته: کیفیت 360p از cdn.pvvstream.pro (نه cdn2) میاد و params اضافه rs/rb داره.

  ─── سرور ───
  - mat6tube.com: uvicorn (FastAPI/Starlette)
  - cdn.pvvstream.pro / cdn2.pvvstream.pro / tr.pvvstream.pro: CDN با Range support
  - بدون Cloudflare!

  ─── کوکی ───
  - csrftoken (Django CSRF)
  - PHPSESSID (legacy session)
  - لازم نیست برای CDN request ها (URL ها signed هستن).

  ─── رفتار CDN ───
  - Accept-Ranges: bytes ✓ (multi-segment download کار می‌کنه)
  - بعضی URL های 240p روی cdn2 (related videos) 403 می‌دن — احتمالاً فقط مال owner فعلی کار می‌کنه
  - HTTP 206 برای Range request ها ✓
  - URL های secure با TTL کوتاه (معمولاً 24 ساعت)

کیفیت‌ها:
  - 240p, 360p, 480p, 720p (و شاید 1080p برای بعضی ویدیوها)

استراتژی دانلود:
  1. fetch صفحه mat6tube با aiohttp (سریع — بدون CF)
  2. fallback به curl_cffi با impersonate=chrome
  3. استخراج window.playlist JSON از HTML
  4. parse sources array
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

logger = logging.getLogger("Mat6tubeHandler")

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
    "mat6tube.com",
    "www.mat6tube.com",
    "m.mat6tube.com",
})

# CDN مجاز (pvvstream.pro variants)
_ALLOWED_CDN_HOSTS = frozenset({
    "cdn.pvvstream.pro",
    "cdn2.pvvstream.pro",
    "tr.pvvstream.pro",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_mat6tube_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به mat6tube هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".mat6tube.com")
    except Exception:
        return False


def _is_cdn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به CDN مجاز هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_CDN_HOSTS or host.endswith(".pvvstream.pro")
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
    # decode \u0026 → &
    url = url.replace("\\u0026", "&")
    url = url.replace("\\/", "/")
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
        title = re.sub(r"\s*[-|@]\s*(?:mat6tube\.com|Mat6tube)\s*$", "", title, flags=re.IGNORECASE)
        if title:
            return title

    # روش 2: <h1>
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        # remove <i class="hd_mark">HD</i>
        title = re.sub(r'<i[^>]*>.*?</i>', '', title, flags=re.IGNORECASE | re.DOTALL).strip()
        if title:
            return title

    # روش 3: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|@]\s*(?:mat6tube\.com|Mat6tube)\s*$", "", title, flags=re.IGNORECASE)
        return title or "Untitled"

    return "Untitled"


def _extract_thumbnail(html: str, playlist: dict = None) -> str:
    """استخراج thumbnail."""
    # روش 1: از playlist image
    if playlist and playlist.get("image"):
        url = playlist["image"]
        if url.startswith("//"):
            url = "https:" + url
        return url

    # روش 2: og:image
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
    """استخراج مدت زمان از JSON-LD یا meta."""
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

    # از ya:ovs:duration
    m = re.search(r'ya:ovs:duration["\']?\s*[:=]\s*["\']?(\d+)', html, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    return None


def _extract_playlist(html: str) -> Optional[dict]:
    """
    استخراج window.playlist JSON از HTML.

    ساختار:
      window.playlist = {...};
    """
    # الگوی اصلی: window.playlist = {...};
    m = re.search(
        r'window\.playlist\s*=\s*(\{.*?\});',
        html, re.DOTALL,
    )
    if not m:
        # الگوی alt بدون semicolon
        m = re.search(
            r'window\.playlist\s*=\s*(\{.*?\})\s*</script>',
            html, re.DOTALL,
        )
        if not m:
            logger.error("window.playlist پیدا نشد در HTML")
            return None

    raw_json = m.group(1)
    # fix \u0026 → &
    cleaned = raw_json.replace("\\u0026", "&").replace("\\/", "/")

    try:
        data = json.loads(cleaned)
        logger.info("window.playlist parsed: %d sources", len(data.get("sources", [])))
        return data
    except json.JSONDecodeError as e:
        logger.error("window.playlist parse failed: %s", e)
        # تلاش با regex fallback
        return _extract_playlist_regex(raw_json)


def _extract_playlist_regex(raw: str) -> Optional[dict]:
    """Fallback: استخراج با regex."""
    sources = []
    # الگو: {"file":"...","label":"720","type":"mp4"} یا با default
    pattern = re.compile(
        r'\{\s*"file"\s*:\s*"([^"]+)"\s*,\s*"label"\s*:\s*"([^"]+)"\s*,\s*"type"\s*:\s*"([^"]+)"(?:\s*,\s*"default"\s*:\s*(true|false))?\s*\}',
        re.DOTALL,
    )
    for m in pattern.finditer(raw):
        file_url = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
        sources.append({
            "file": file_url,
            "label": m.group(2),
            "type": m.group(3),
            "default": m.group(4) == "true" if m.group(4) else False,
        })

    if not sources:
        return None

    logger.info("Regex fallback extracted %d sources", len(sources))
    return {"sources": sources, "image": "", "trusted": True}


def _extract_video_sources(html: str) -> List[dict]:
    """
    استخراج URL های ویدیو از HTML.

    اولویت 1: window.playlist JSON
    اولویت 2: regex fallback برای URL های pvvstream.pro

    Returns:
        list of dicts: [{label, url, height, quality_key, method, is_hd}, ...]
    """
    sources = []
    seen_urls = set()

    playlist = _extract_playlist(html)

    if playlist:
        for src in playlist.get("sources", []):
            file_url = src.get("file", "").strip()
            if not file_url:
                continue
            # تمیزکاری
            file_url = _clean_url(file_url)

            # protocol-relative
            if file_url.startswith("//"):
                file_url = "https:" + file_url

            # فقط CDN مجاز
            if not _is_cdn_url(file_url):
                logger.debug("Skipping non-CDN URL: %s", file_url[:80])
                continue

            if file_url in seen_urls:
                continue
            seen_urls.add(file_url)

            # کیفیت از label یا filename
            label_str = str(src.get("label", "")).strip()
            try:
                height = int(label_str)
                quality_text = f"{height}p"
            except (ValueError, TypeError):
                # از filename
                m = re.search(r'vid_(\d{3,4})p\.mp4', file_url, re.IGNORECASE)
                if m:
                    height = int(m.group(1))
                    quality_text = f"{height}p"
                else:
                    # fallback
                    if "1080" in file_url:
                        height, quality_text = 1080, "1080p"
                    elif "720" in file_url:
                        height, quality_text = 720, "720p"
                    elif "480" in file_url:
                        height, quality_text = 480, "480p"
                    elif "360" in file_url:
                        height, quality_text = 360, "360p"
                    elif "240" in file_url:
                        height, quality_text = 240, "240p"
                    else:
                        height = 480
                        quality_text = label_str or "default"

            is_hd = height >= 720
            label = f"📺 MP4 {quality_text}"

            sources.append({
                "label": label,
                "url": file_url,
                "height": height,
                "quality_key": quality_text.lower(),
                "method": "playlist",
                "is_hd": is_hd,
            })
            logger.info("Found source: %s (%s)", quality_text, file_url[:100])

    # ─── روش 2: regex fallback برای pvvstream.pro URLs ───
    cdn_pattern = re.compile(
        r'(https?://[a-z0-9-]+\.pvvstream\.pro/videos/[^\s"\'<>\\]+?\.mp4[^\s"\'<>\\]*)',
        re.IGNORECASE,
    )
    for m in cdn_pattern.finditer(html):
        url = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
        url = _clean_url(url)
        if url in seen_urls:
            continue
        # فقط ویدیوهای اصلی (با vid_ یا tr_) نه preview
        if "preview" in url.lower():
            continue
        seen_urls.add(url)

        # کیفیت از filename
        m_q = re.search(r'vid_(\d{3,4})p\.mp4', url, re.IGNORECASE)
        if m_q:
            height = int(m_q.group(1))
            quality_text = f"{height}p"
        else:
            # ترانهه برای tr_ (trailer)
            continue

        is_hd = height >= 720
        label = f"📺 MP4 {quality_text}"
        sources.append({
            "label": label,
            "url": url,
            "height": height,
            "quality_key": quality_text.lower(),
            "method": "cdn_regex",
            "is_hd": is_hd,
        })
        logger.info("Found via CDN regex: %s (%s)", quality_text, url[:100])

    # مرتب‌سازی: بالاترین کیفیت اول
    sources.sort(key=lambda q: q.get("height", 0), reverse=True)
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
                        if "window.playlist" in html or "vid_" in html:
                            logger.info("Page fetched via aiohttp: %s (size=%d)", url[:80], len(html))
                            return html, local_jar, ""
                        else:
                            logger.warning("aiohttp: 200 ولی playlist پیدا نشد")
                    logger.warning("aiohttp fetch: HTTP %d", resp.status)
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
                    if "window.playlist" in text or "vid_" in text:
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
                    else:
                        logger.warning("curl_cffi: 200 ولی playlist پیدا نشد")
                logger.warning("curl_cffi fetch: HTTP %d", resp.status_code)
        except Exception as e:
            logger.warning(f"curl_cffi fetch error: {e}")

    return None, jar, "Failed to fetch page"


# ─── Main API: extract qualities ──────────────────────────────────────────


async def extract_mat6tube_qualities(url, progress_cb=None):
    """استخراج کیفیت‌های ویدیو."""
    if not is_mat6tube_url(url):
        return [], "Invalid URL — host not allowed", {}

    if progress_cb:
        await progress_cb("🔄 **دریافت اطلاعات صفحه...**")

    jar = CookieJar(unsafe=True)
    html, jar, error = await _fetch_page(url, jar, method="all")

    if not html:
        logger.error("Failed to fetch page: %s", error)
        return [], f"خطا در دریافت صفحه: {error}", {}

    title = _extract_title(html)
    duration = _extract_duration(html)
    sources = _extract_video_sources(html)
    playlist = _extract_playlist(html)
    thumbnail = _extract_thumbnail(html, playlist)

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

    pvvstream.pro از Range پشتیبانی می‌کنه.
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
        # Referer از mat6tube مهمه (برای بعضی CDN ها)
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
                    elif r.status == 410:
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
                        elif r.status == 403:
                            return False, "HTTP_403", 0
                        elif r.status == 410:
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
            "[DL-M6T] Work-queue: %d chunks, %d workers, total=%d",
            total_chunks, num_workers, content_length,
        )

        # pre-allocate file با aiofiles
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

        # session اشتراکی + file handle باز نگه داریم
        shared_timeout = ClientTimeout(total=600, connect=30, sock_read=120)
        connector = TCPConnector(
            limit=CONNECTOR_LIMIT, limit_per_host=CONNECTOR_LIMIT_PER_HOST,
            keepalive_timeout=60, enable_cleanup_closed=True,
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

                for attempt in range(MAX_RETRIES):
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
                            "[DL-M6T] W%d c%d attempt %d failed: %s",
                            worker_id, c_idx, attempt + 1, e,
                        )
                        if attempt < MAX_RETRIES - 1:
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

            # تشخیص failure های واقعی
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
                    "[DL-M6T] %d worker failures, %d chunk failures",
                    len(real_failures), len(failed_chunks),
                )
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(real_failures)+len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-M6T] Work-queue error: {e}", exc_info=True)
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
            "[DL-M6T] Multi-segment DONE | size=%s | time=%.1fs | avg_speed=%.1f MB/s",
            _format_size(file_size), elapsed, avg_speed,
        )
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-M6T] Multi-segment error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Download: Single connection (fallback) ───────────────────────────────


async def _download_single_aiohttp(url, filepath, referer, cookies, progress_cb, dl_id=""):
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
            "--add-header", "Referer:https://mat6tube.com/",
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
        logger.info(f"[DL-M6T] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-M6T] yt-dlp error: {e}", exc_info=True)
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


async def download_mat6tube_video(
    page_url, video_url, filepath, progress_cb=None, cookies=None, dl_id="",
    quality_key="",
):
    """دانلود ویدیو از mat6tube با کیفیت انتخاب شده."""
    if not is_mat6tube_url(page_url):
        return False, "URL host not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0
    if progress_cb is None:
        async def _noop(msg): pass
        progress_cb = _noop
    if dl_id and dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}
    referer = "https://mat6tube.com/"
    if not cookies:
        cookies = {}

    # ── روش 1: multi-segment ──
    logger.info(f"[DL-M6T] Attempt 1: multi-segment ({MULTI_SEGMENT_WORKERS} workers)")
    success, error, size = await _download_multi_segment(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0

    # اگه URL منقضی شده یا 403، یه URL تازه بگیر
    if error in ("HTTP_403", "URL_EXPIRED"):
        logger.info(f"[DL-M6T] {error}, refreshing session...")
        if progress_cb:
            await progress_cb("🔄 **Refreshing session...**")
        try:
            new_sources, _, new_info = await extract_mat6tube_qualities(page_url, progress_cb=None)
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
                logger.info("[DL-M6T] Got fresh URL")
        except Exception as e:
            logger.warning(f"[DL-M6T] refresh failed: {e}")

        # retry multi-segment با URL تازه
        success, error, size = await _download_multi_segment(
            video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
        )
        if success:
            return True, "", size
    logger.info(f"[DL-M6T] Multi-segment failed: {error}")
    _cleanup_file(filepath)

    # ── روش 2: single-connection ──
    logger.info("[DL-M6T] Attempt 2: single-connection")
    success, error, size = await _download_single_aiohttp(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    logger.info(f"[DL-M6T] Single failed: {error}")
    _cleanup_file(filepath)

    # ─ـ روش 3: yt-dlp ──
    logger.info("[DL-M6T] Attempt 3: yt-dlp on page URL")
    success, error, size = await _download_with_ytdlp(
        page_url, filepath, progress_cb, quality_key=quality_key,
    )
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


async def download_mat6tube_direct(
    url, filepath, progress_cb=None, video_url="", quality="high", dl_id="",
):
    """Wrapper برای سازگاری با bot architecture."""
    if not video_url:
        qualities, title, info = await extract_mat6tube_qualities(url, progress_cb)
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
        qualities, title, info = await extract_mat6tube_qualities(url, progress_cb)
        cookies = info.get("cookies", {}) if info else {}
        quality_key = quality

    return await download_mat6tube_video(
        url, video_url, filepath, progress_cb,
        cookies=cookies, dl_id=dl_id, quality_key=quality_key,
    )


# ─── Self-test ─────────────────────────────────────────────────────────────


async def _self_test():
    """تست خودي هندلر."""
    test_url = "https://mat6tube.com/watch/-185044521_456239719"
    print(f"\n{'═' * 80}")
    print(f"Self-test: {test_url}")
    print(f"{'═' * 80}\n")

    async def progress(msg):
        print(f"  → {msg}")

    sources, title, info = await extract_mat6tube_qualities(test_url, progress_cb=progress)

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
