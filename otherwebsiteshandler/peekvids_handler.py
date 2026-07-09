"""
peekvids_handler.py
───────────────────
استخراج و دانلود ویدیو از peekvids.com

روش کار (بر اساس تحلیل واقعی صفحه):

  ─── ساختار صفحه ───
  صفحه ویدیو HTML شامل:
    1. یه <video> tag با یه <source> که type="application/x-mpegURL" (HLS)
    2. URL خاص به‌صورت urlset:
       https://vs38.userscontent.net/uls2/,PATH1,PATH2,PATH3,.urlset/master.m3u8?seclink=...&sectime=...
    3. Player: Fluid Player + HLS.js

  ─── ساختار URL HLS ───
  master.m3u8 (یک فایل playlist که چند variant داره):

    #EXTM3U
    #EXT-X-VERSION:4
    #EXT-X-INDEPENDENT-SEGMENTS

    #EXT-X-STREAM-INF:BANDWIDTH=547722,RESOLUTION=480x270,FRAME-RATE=24,CODECS="avc1.4d4015,mp4a.40.2"
    https://vs38.userscontent.net/uls2/670/9223/HASH1.mp4/index-v1-a1.m3u8

    #EXT-X-STREAM-INF:BANDWIDTH=1517794,RESOLUTION=640x360,FRAME-RATE=24,CODECS="avc1.4d401e,mp4a.40.2"
    https://vs38.userscontent.net/uls2/670/9223/HASH2.mp4/index-v1-a1.m3u8

    #EXT-X-STREAM-INF:BANDWIDTH=2248584,RESOLUTION=1280x720,FRAME-RATE=24,CODECS="avc1.4d401f,mp4a.40.2"
    https://vs38.userscontent.net/uls2/670/9223/HASH3.mp4/index-v1-a1.m3u8

  هر variant یه playlist از TS segments داره:
    seg-1-v1-a1.ts, seg-2-v1-a1.ts, ...

  ─── سرور ───
  - www.peekvids.com: nginx (بدون Cloudflare!)
  - vs38.userscontent.net: CDN با Range support
  - بدون hotlink protection (با/بدون Referer کار می‌کنه)
  - URL امضا‌دار با TTL (seclink + sectime)

  ─── کوکی ───
  - PHPSESSID برای session persistence (لازم نیست برای CDN)

کیفیت‌ها (از master.m3u8 استخراج می‌شن):
  - معمولاً 270p, 360p, 720p (ممکنه 1080p هم باشه برای بعضی ویدیوها)

استراتژی دانلود:
  1. fetch صفحه peekvids با aiohttp
  2. استخراج <source> URL (HLS master.m3u8) از HTML
  3. fetch master.m3u8 برای پیدا کردن variant URLs و resolutions
  4. انتخاب variant با توجه به کیفیت درخواستی
  5. دانلود با ffmpeg (بهترین گزینه برای HLS) — parallel + merge
  6. fallback به yt-dlp روی URL صفحه

نکته مهم: چون این سایت HLS-only هست، نمی‌تونیم از multi-segment download مستقیم
استفاده کنیم. ffmpeg بهترین گزینه‌ست چون:
  - می‌تونه چند segment رو parallel دانلود کنه
  - TS segments رو به MP4 تبدیل و merge می‌کنه
  - از seclink/sectime URL مستقیماً استفاده می‌کنه

وابستگی‌ها:
    pip install aiohttp aiofiles curl_cffi yt-dlp
    apt install ffmpeg  (یا brew install ffmpeg)
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

logger = logging.getLogger("PeekVidsHandler")

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
MAX_RETRIES = 3
RETRY_DELAY = 2.0

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset({
    "peekvids.com",
    "www.peekvids.com",
    "m.peekvids.com",
})

# CDN مجاز ( HLS hosts )
_ALLOWED_CDN_HOSTS = frozenset({
    "userscontent.net",
})
# یادآوری: subdomain های مختلفی هستن (vs38, vs39, ...) — پس endswith چک می‌کنیم

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_peekvids_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به peekvids هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".peekvids.com")
    except Exception:
        return False


def _is_cdn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به CDN مجاز هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_CDN_HOSTS or host.endswith(".userscontent.net")
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
    url = url.replace("\\/", "/").replace("\\u0026", "&")
    url = unquote(url).replace("&amp;", "&")
    url = re.sub(r'[\\/]+$', '', url)
    url = url.rstrip("',\"")
    return url.strip()


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
        title = re.sub(r"\s*[-|@]\s*(?:peekvids\.com|PeekVids)\s*$", "", title, flags=re.IGNORECASE)
        if title:
            return title

    # روش 2: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|@]\s*(?:peekvids\.com|PeekVids)\s*$", "", title, flags=re.IGNORECASE)
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
    """استخراج مدت زمان از JSON-LD."""
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


def _extract_hls_source_url(html: str) -> str:
    """
    استخراج HLS source URL از HTML.

    ساختار:
      <source src="https://vs38.userscontent.net/uls2/,...,.urlset/master.m3u8?seclink=...&sectime=..." type="application/x-mpegURL"/>

    یا به‌صورت JS:
      sources: [{"file":"https://...","type":"application/x-mpegURL"}]
    """
    # روش 1: <source> tag با type=application/x-mpegURL
    source_pattern = re.compile(
        r'<source\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*\btype=["\']application/x-mpegURL["\']',
        re.IGNORECASE,
    )
    for m in source_pattern.finditer(html):
        url = _clean_url(m.group(1))
        if "master.m3u8" in url or ".m3u8" in url:
            return url

    # روش 2: type first, then src
    source_pattern2 = re.compile(
        r'<source\b[^>]*\btype=["\']application/x-mpegURL["\'][^>]*\bsrc=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    for m in source_pattern2.finditer(html):
        url = _clean_url(m.group(1))
        if "master.m3u8" in url or ".m3u8" in url:
            return url

    # روش 3: regex مستقیم برای master.m3u8 با urlset
    m = re.search(
        r'(https?://[^\s"\'<>]+?\.urlset/master\.m3u8\?[^\s"\'<>]+)',
        html, re.IGNORECASE,
    )
    if m:
        return _clean_url(m.group(1))

    # روش 4: regex مستقیم برای هر m3u8
    m = re.search(
        r'(https?://[a-z0-9-]+\.userscontent\.net[^\s"\'<>]+?\.m3u8[^\s"\'<>]*)',
        html, re.IGNORECASE,
    )
    if m:
        return _clean_url(m.group(1))

    return ""


def _parse_master_m3u8(content: str, base_url: str = "") -> List[dict]:
    """
    Parse master.m3u8 برای استخراج variant playlists.

    Returns:
        list of dicts: [{url, resolution, bandwidth, height, quality_key, is_hd}, ...]
    """
    variants = []
    lines = content.split("\n")

    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue

        # parse attributes
        attrs = {}
        attr_str = line[len("#EXT-X-STREAM-INF:"):]
        # اگه URI در همون خط باشه (I-FRAME-STREAM-INF) skip می‌کنیم
        for attr in re.finditer(r'(\w+)=(?:"([^"]+)"|([^,]+))', attr_str):
            key = attr.group(1)
            val = attr.group(2) or attr.group(3)
            attrs[key] = val

        # URL در خط بعدی
        if i + 1 < len(lines):
            variant_url = lines[i + 1].strip()
            if not variant_url or variant_url.startswith("#"):
                continue

            # resolve relative URL
            if variant_url.startswith("http"):
                url = variant_url
            elif variant_url.startswith("/"):
                parsed = urlparse(base_url)
                url = f"{parsed.scheme}://{parsed.netloc}{variant_url}"
            else:
                url = urljoin(base_url, variant_url)

            # extract resolution
            resolution = attrs.get("RESOLUTION", "")
            bandwidth = attrs.get("BANDWIDTH", "")
            try:
                height = int(resolution.split("x")[1]) if "x" in resolution else 0
            except (ValueError, IndexError):
                height = 0

            quality_text = f"{height}p" if height > 0 else "auto"
            is_hd = height >= 720

            variants.append({
                "url": url,
                "resolution": resolution,
                "bandwidth": bandwidth,
                "height": height,
                "quality_key": quality_text.lower(),
                "is_hd": is_hd,
            })

    # sort by height descending
    variants.sort(key=lambda v: v.get("height", 0), reverse=True)
    return variants


async def _fetch_master_m3u8(
    url: str,
    referer: str = "https://www.peekvids.com/",
    cookies: Optional[dict] = None,
) -> Tuple[Optional[str], str]:
    """
    Fetch master.m3u8 content.

    نکته مهم: peekvids CDN از TLS fingerprinting استفاده می‌کنه.
    aiohttp با fingerprint پایتون شناخته می‌شه و 403 می‌گیره.
    باید از curl_cffi با impersonate=chrome استفاده کنیم.
    """
    if not _check_curl_cffi():
        logger.error("curl_cffi not installed — required for peekvids CDN")
        return None, "curl_cffi not installed"

    from curl_cffi.requests import AsyncSession

    # curl_cffi با impersonate=chrome TLS مرورگر رو شبیه‌سازی می‌کنه
    # نکته مهم: Sec-Fetch headers نباید set بشن چون curl_cffi خودش مدیریت می‌کنه.
    # اگه Sec-Fetch-Dest=document set کنیم، CDN ممکنه 403 بده.
    # فقط هدرهای ضروری رو set می‌کنیم.
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        # User-Agent رو set نمی‌کنیم چون curl_cffi با impersonate=chrome خودش
        # یه UA مناسب set می‌کنه.
    }

    try:
        async with AsyncSession() as s:
            # نکته: peekvids CDN گاهی با PHPSESSID کوکی 403 می‌ده.
            # seclink به TLS fingerprint (curl_cffi/impersonate) متصله، نه به کوکی.
            # پس کوکی‌ها رو set نمی‌کنیم.

            resp = await s.get(
                url,
                impersonate="chrome",
                headers=headers,
                allow_redirects=True,
                timeout=15,
            )
            if resp.status_code == 200:
                content = resp.text
                logger.info("master.m3u8 fetched via curl_cffi: %d chars", len(content))
                return content, ""
            logger.warning("master.m3u8 curl_cffi fetch: HTTP %d", resp.status_code)
            return None, f"HTTP {resp.status_code}"
    except Exception as e:
        logger.warning(f"master.m3u8 curl_cffi error: {e}")
        return None, str(e)


async def _extract_video_sources(
    html: str,
    referer: str = "https://www.peekvids.com/",
    cookies: Optional[dict] = None,
) -> List[dict]:
    """
    استخراج variant URLs از HTML.

    مراحل:
      1. پیدا کردن HLS source URL از HTML
      2. fetch master.m3u8 با curl_cffi (TLS fingerprinting)
      3. parse variant playlists
      4. اگه m3u8 fetch fail شد، فقط master URL رو برمی‌گردونیم (yt-dlp خودش دانلود می‌کنه)

    Returns:
        list of dicts: [{label, url, height, quality_key, method, is_hd, bandwidth}, ...]
    """
    sources = []

    master_url = _extract_hls_source_url(html)
    if not master_url:
        logger.error("No HLS source URL found in HTML")
        return []

    logger.info("Found HLS master URL: %s", master_url[:100])

    # fetch master.m3u8 با cookies از page fetch
    content, error = await _fetch_master_m3u8(master_url, referer=referer, cookies=cookies)

    if not content:
        # fallback: اگه m3u8 fetch fail شد، یه source "auto" برمی‌گردونیم
        # که yt-dlp خودش کیفیت رو انتخاب می‌کنه
        logger.warning(
            "master.m3u8 fetch failed (%s) — returning 'auto' quality for yt-dlp",
            error
        )
        sources.append({
            "label": "📺 MP4 auto (yt-dlp)",
            "url": master_url,
            "height": 0,
            "quality_key": "auto",
            "method": "master_url_fallback",
            "is_hd": False,
            "bandwidth": "",
            "master_url": master_url,
        })
        return sources

    # parse variants
    variants = _parse_master_m3u8(content, base_url=master_url)
    if not variants:
        logger.error("No variants found in master.m3u8")
        # fallback به auto
        sources.append({
            "label": "📺 MP4 auto (yt-dlp)",
            "url": master_url,
            "height": 0,
            "quality_key": "auto",
            "method": "master_url_fallback",
            "is_hd": False,
            "bandwidth": "",
            "master_url": master_url,
        })
        return sources

    for v in variants:
        height = v["height"]
        quality_text = v["quality_key"]
        # convert quality_key from "720p" to "720p"
        if not quality_text.endswith("p") and height > 0:
            quality_text = f"{height}p"
        label = f"📺 MP4 {quality_text}"
        sources.append({
            "label": label,
            "url": v["url"],  # variant playlist URL
            "height": height,
            "quality_key": quality_text.lower(),
            "method": "hls_variant",
            "is_hd": v["is_hd"],
            "bandwidth": v.get("bandwidth", ""),
            "master_url": master_url,
        })
        logger.info(
            "Found variant: %s (%s, %s bps)",
            quality_text, v.get("resolution"), v.get("bandwidth")
        )

    return sources


# ─── Fetch Page ───────────────────────────────────────────────────────────


async def _fetch_page(url, jar=None, method="aiohttp"):
    """fetch صفحه با fallback.

    نکته مهم: peekvids seclink به TLS fingerprint متصله.
    اگه با aiohttp fetch کنیم، seclink فقط با aiohttp کار می‌کنه، نه curl_cffi.
    پس باید page و m3u8 fetch هر دو با curl_cffi (یا هر دو با aiohttp) انجام بشن.
    اول curl_cffi رو امتحان می‌کنیم چون برای m3u8 لازمه.
    """
    headers = dict(_DEFAULT_HEADERS)

    # روش 1: curl_cffi (preferred برای peekvids — به‌خاطر TLS fingerprinting)
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
                    if "userscontent.net" in text or "m3u8" in text:
                        logger.info("Page fetched via curl_cffi: %s (size=%d)", url[:80], len(text))
                        # extract cookies
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
                        logger.warning("curl_cffi: 200 ولی source پیدا نشد")
                logger.warning("curl_cffi fetch: HTTP %d", resp.status_code)
        except Exception as e:
            logger.warning(f"curl_cffi fetch error: {e}")

    # روش 2: aiohttp (fallback)
    if method in ("aiohttp", "all"):
        try:
            local_jar = jar or CookieJar(unsafe=True)
            timeout = ClientTimeout(total=30, connect=10)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers, cookie_jar=local_jar) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="replace")
                        if "userscontent.net" in html or "m3u8" in html:
                            logger.info("Page fetched via aiohttp: %s (size=%d)", url[:80], len(html))
                            return html, local_jar, ""
                        else:
                            logger.warning("aiohttp: 200 ولی source پیدا نشد")
                    logger.warning("aiohttp fetch: HTTP %d", resp.status)
        except Exception as e:
            logger.warning(f"aiohttp fetch error: {e}")

    return None, jar, "Failed to fetch page"


# ─── Main API: extract qualities ──────────────────────────────────────────


async def extract_peekvids_qualities(url, progress_cb=None):
    """استخراج کیفیت‌های ویدیو."""
    if not is_peekvids_url(url):
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
        await progress_cb("🔎 **استخراج کیفیت‌های HLS...**")

    cookies = {}
    if jar:
        for cookie in jar:
            cookies[cookie.key] = cookie.value
    logger.info("Cookies passed to extract_video_sources: %s", list(cookies.keys()))

    sources = await _extract_video_sources(html, referer=url, cookies=cookies)

    if not sources:
        logger.error("No video sources found")
        return [], "URL ویدیو در صفحه پیدا نشد", {}

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


# ─── Download: ffmpeg (HLS) ───────────────────────────────────────────────


active_downloads: dict = {}


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


async def _download_with_ffmpeg(
    hls_url, filepath, progress_cb, dl_id="",
    referer="https://www.peekvids.com/",
    duration_hint=0,
    cookies: Optional[dict] = None,
):
    """
    دانلود HLS با ffmpeg.

    نکته مهم: peekvids CDN از TLS fingerprinting استفاده می‌کنه.
    ffmpeg نمی‌تونه TLS مرورگر رو شبیه‌سازی کنه، پس ممکنه 403 بگیره.
    اگه ffmpeg fail شد، باید yt-dlp با --impersonate chrome استفاده کنیم.
    """
    if not shutil.which("ffmpeg"):
        return False, "ffmpeg not installed", 0

    await progress_cb("📥 **Downloading via ffmpeg (HLS)...**")

    # تبدیل cookies به header string برای ffmpeg
    cookies_str = ""
    if cookies:
        cookies_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # ffmpeg args
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "warning",
        "-user_agent", _USER_AGENT,
        "-referer", referer,
        "-headers", "Accept: */*\r\nAccept-Language: en-US,en;q=0.9\r\n",
    ]
    if cookies_str:
        cmd.extend(["-cookies", cookies_str])
    cmd.extend([
        "-i", hls_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        filepath,
    ])

    logger.info("[DL-PV] ffmpeg cmd: %s", " ".join(cmd[:10]) + " ...")

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        #ffmpeg progress رو روی stderr می‌نویسه (با -progress pipe:1 یا با parsing log)
        # برای گرفتن progress، باید stderr رو بخونیم
        last_update = 0.0
        start_time = time.time()
        last_size = 0
        last_time_mono = time.time()

        # ذخیره stderr برای debugging
        stderr_buffer = []

        async def _read_stderr():
            nonlocal last_update, last_size, last_time_mono
            try:
                while True:
                    if active_downloads.get(dl_id, {}).get("cancelled"):
                        process.kill()
                        return
                    line = await process.stderr.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace").strip()
                    if text:
                        stderr_buffer.append(text)
                    # parse for size/time
                    # ffmpeg خطوطی مثل این چاپ می‌کنه:
                    # frame=  123 fps= 30 q=-1.0 size=    1024kB time=00:00:05.12 ...
                    now = time.time()
                    if now - last_update >= PROGRESS_INTERVAL:
                        last_update = now
                        # فایل size روی دیسک
                        try:
                            current_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                        except OSError:
                            current_size = 0

                        if current_size > 0:
                            # محاسبه سرعت
                            elapsed = now - last_time_mono
                            speed = (current_size - last_size) / elapsed if elapsed > 0 else 0
                            last_size = current_size
                            last_time_mono = now

                            dl_mb = current_size / 1024 / 1024
                            speed_mb = min(speed / 1024 / 1024, 999)

                            # اگه duration Hint داریم، درصد رو از time خط ffmpeg می‌گیریم
                            pct = 0
                            eta_str = "?"
                            time_match = re.search(r'time=(\d+):(\d+):(\d+\.\d+)', text)
                            if time_match and duration_hint > 0:
                                h, m, s = int(time_match.group(1)), int(time_match.group(2)), float(time_match.group(3))
                                current_time = h * 3600 + m * 60 + s
                                pct = (current_time / duration_hint) * 100
                                if speed > 0 and current_time < duration_hint:
                                    eta_secs = int((duration_hint - current_time) * (current_size / max(speed * (current_time or 1), 1)))
                                    eta_m, eta_s = divmod(min(eta_secs, 9999), 60)
                                    eta_str = f"{eta_m}:{eta_s:02d}"
                                filled = int(pct / 5)
                                bar = "█" * filled + "░" * (20 - filled)
                                try:
                                    await progress_cb(
                                        f"📥 **Downloading (HLS)...**\n`[{bar}]`\n"
                                        f"💾 {dl_mb:.1f} MB  •  ⚡ {speed_mb:.1f} MB/s\n"
                                        f"📊 {pct:.1f}%  •  ⏱ ETA: {eta_str}"
                                    )
                                except Exception:
                                    pass
                            else:
                                try:
                                    await progress_cb(
                                        f"📥 **Downloading (HLS)...**\n"
                                        f"💾 {dl_mb:.1f} MB  •  ⚡ {speed_mb:.1f} MB/s"
                                    )
                                except Exception:
                                    pass
            except asyncio.CancelledError:
                process.kill()
                raise
            except Exception as e:
                logger.warning(f"[DL-PV] stderr read error: {e}")

        # اجرای stderr reader در background
        stderr_task = asyncio.create_task(_read_stderr())

        # صبر برای اتمام process
        await process.wait()
        try:
            stderr_task.cancel()
        except Exception:
            pass

        if active_downloads.get(dl_id, {}).get("cancelled"):
            _cleanup_file(filepath)
            return False, "Cancelled by user", 0

        if process.returncode != 0:
            err_tail = "\n".join(stderr_buffer[-5:])[:300]
            logger.error(f"[DL-PV] ffmpeg failed (rc={process.returncode}): {err_tail}")
            _cleanup_file(filepath)
            return False, f"ffmpeg failed: {err_tail[:200]}", 0

        file_size = os.path.getsize(filepath)
        if file_size < MIN_VALID_VIDEO_SIZE:
            _cleanup_file(filepath)
            return False, f"File too small ({file_size} bytes)", 0

        elapsed = time.time() - start_time
        avg_speed = file_size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        logger.info(
            "[DL-PV] ffmpeg DONE | size=%s | time=%.1fs | avg_speed=%.1f MB/s",
            _format_size(file_size), elapsed, avg_speed,
        )
        return True, "", file_size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-PV] ffmpeg error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Download: yt-dlp (fallback) ──────────────────────────────────────────


async def _download_with_ytdlp(url, filepath, progress_cb, quality_key=""):
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0
    has_curl_cffi = _check_curl_cffi()
    await progress_cb("📥 **Fallback: yt-dlp...**")

    # اگه URL یه m3u8 هست (نه صفحه)، باید extractor=generic استفاده کنیم
    is_m3u8 = ".m3u8" in url
    format_selector = "best"
    if quality_key in ("720p", "480p", "1080p", "360p", "270p"):
        # برای HLS، yt-dlp از height استفاده می‌کنه
        height = quality_key.replace("p", "")
        format_selector = f"[height<={height}]/best"

    try:
        cmd = [
            "yt-dlp", "--no-warnings", "--progress", "--newline",
            "--no-check-certificates", "-f", format_selector,
            "--concurrent-fragments", "16",
            "--retries", "10", "--fragment-retries", "10",
            "--buffer-size", "16K",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "--add-header", "Referer:https://www.peekvids.com/",
        ]
        if is_m3u8:
            cmd.extend(["--force-generic-extractor"])
        if has_curl_cffi:
            cmd.extend(["--impersonate", "chrome"])
        cmd.extend(["-o", filepath])
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
        logger.info(f"[DL-PV] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-PV] yt-dlp error: {e}", exc_info=True)
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
    for ext in (".mp4", ".mkv", ".webm", ".ts", ".m4a"):
        candidate = base + ext
        if os.path.exists(candidate):
            return candidate
    return None


# ─── Public API ────────────────────────────────────────────────────────────


async def download_peekvids_video(
    page_url, hls_url, filepath, progress_cb=None, cookies=None, dl_id="",
    quality_key="", duration_hint=0,
):
    """دانلود ویدیو از peekvids با HLS URL انتخاب شده."""
    if not is_peekvids_url(page_url):
        return False, "URL host not allowed", 0
    if not hls_url:
        return False, "Empty HLS URL", 0
    if progress_cb is None:
        async def _noop(msg): pass
        progress_cb = _noop
    if dl_id and dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}

    referer = page_url

    # ── روش 1: yt-dlp با --impersonate chrome (بهترین برای peekvids) ──
    # چون CDN از TLS fingerprinting استفاده می‌کنه، yt-dlp با impersonate
    # بهترین گزینه‌ست. ffmpeg نمی‌تونه TLS رو شبیه‌سازی کنه.
    # نکته: hls_url (master.m3u8 URL) رو به yt-dlp می‌دیم، نه page_url.
    logger.info("[DL-PV] Attempt 1: yt-dlp with --impersonate chrome on HLS URL")
    success, error, size = await _download_with_ytdlp(
        hls_url, filepath, progress_cb, quality_key=quality_key,
    )
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0
    logger.info(f"[DL-PV] yt-dlp failed: {error}")
    _cleanup_file(filepath)

    # ─ـ روش 2: ffmpeg (fallback — ممکنه 403 بگیره) ──
    logger.info("[DL-PV] Attempt 2: ffmpeg (HLS)")
    success, error, size = await _download_with_ffmpeg(
        hls_url, filepath, progress_cb, dl_id=dl_id,
        referer=referer, duration_hint=duration_hint, cookies=cookies,
    )
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


async def download_peekvids_direct(
    url, filepath, progress_cb=None, video_url="", quality="high", dl_id="",
):
    """Wrapper برای سازگاری با bot architecture."""
    if not video_url:
        qualities, title, info = await extract_peekvids_qualities(url, progress_cb)
        if not qualities:
            return False, title or "Extraction failed", 0
        selected = None
        for q in qualities:
            if q.get("quality_key") == quality:
                selected = q
                break
        if not selected:
            if quality in ("high", "best", "1080p", "720p"):
                hd = [q for q in qualities if q.get("is_hd")]
                selected = hd[0] if hd else qualities[0]
            elif quality in ("low", "worst", "270p", "240p"):
                selected = qualities[-1]
            elif quality in ("medium", "360p", "480p"):
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
        duration_hint = info.get("duration", 0) if info else 0
    else:
        qualities, title, info = await extract_peekvids_qualities(url, progress_cb)
        duration_hint = info.get("duration", 0) if info else 0
        quality_key = quality

    return await download_peekvids_video(
        url, video_url, filepath, progress_cb,
        cookies=None, dl_id=dl_id, quality_key=quality_key,
        duration_hint=duration_hint,
    )


# ─── Self-test ─────────────────────────────────────────────────────────────


async def _self_test():
    """تست خودي هندلر."""
    test_url = "https://www.peekvids.com/v/610002/legqkEABErC"
    print(f"\n{'═' * 80}")
    print(f"Self-test: {test_url}")
    print(f"{'═' * 80}\n")

    async def progress(msg):
        print(f"  → {msg}")

    sources, title, info = await extract_peekvids_qualities(test_url, progress_cb=progress)

    print(f"\n  Title: {title}")
    print(f"  Thumbnail: {info.get('thumbnail', '')[:120]}")
    print(f"  Duration: {info.get('duration', '?')}s")
    print(f"\n  Sources ({len(sources)}):")
    for s in sources:
        print(f"    [{s['quality_key']:6s}] {s['url'][:120]} ({s['method']})")
        if s.get('bandwidth'):
            print(f"             bandwidth={s['bandwidth']} bps, resolution={s.get('height', '?')}p")

    return sources, title, info


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_self_test())
