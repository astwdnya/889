"""
sxyprn_handler.py
─────────────────
استخراج و دانلود ویدیو از sxyprn.net (و sxyprn.com)

روش کار (بر اساس تحلیل واقعی صفحه و JS):

  ─── ساختار صفحه ───
  صفحه ویدیو HTML شامل:
    1. یه <video id='player_el' data-postid='HASH' data-mgfs='183797173' src=''> (src خالی!)
    2. یه <span class='vidsnfo' data-vnfo='{"POST_ID":"/cdn/c5/.../filename.vid"}'>
       که URL رمزنگاری‌شده ویدیو رو داره.

  ─── نکته مهم: URL Transformation ───
  ویدیو URL از data-vnfo استخراج می‌شه و سپس با تابع getvsrc() در main2.js
  transform می‌شه:

    var tmp = src.split("/");
    tmp[1] += "5" + "/" + boo(ssut51(tmp[6]), ssut51(tmp[7]));
    tmp = preda(tmp);
    var src = tmp.join("/");

  توابع:
    - ssut51(arg): sum of all digits in arg (extract digits, sum them)
    - boo(ss, es): base64(ss + "-" + host + "-" + es) with +/= replaced
    - preda(arg): arg[5] -= ssut51(arg[6]) + ssut51(arg[7])

  مثال:
    data-vnfo: /cdn/c5/HASH1/HASH2/1783632365/AUTHOR_ID/FILENAME.vid
    پس از transform:
      /cdn5/{base64}/c5/HASH1/HASH2/{1783632365 - digit_sum(AUTHOR_ID) - digit_sum(FILENAME)}/AUTHOR_ID/FILENAME.vid

  ─── سرور ───
  - sxyprn.net: پشت Cloudflare
  - URL نهایی روی خود sxyprn.net سرو می‌شه (نه CDN جدا)
  - Range request پشتیبانی می‌شه (HTTP 206)
  - Content-Type: video/mp4

  ─── کوکی ───
  - PHPSESSID برای session persistence (لازم نیست برای دانلود)

کیفیت‌ها:
  - فقط یه کیفیت (معمولاً 720p HD یا 480p SD)
  - info از HTML: "duration:18:40 · resolution:HD720 · bitrate:1312 kb/s · size:175 MB"

استراتژی دانلود:
  1. fetch صفحه sxyprn با aiohttp (با CF ممکنه fail بشه — fallback به curl_cffi)
  2. fallback به curl_cffi با impersonate=chrome
  3. استخراج data-vnfo JSON از HTML
  4. اعمال transformation (getvsrc) برای تولید URL نهایی
  5. multi-segment download با 32 workers
  6. fallback به single-connection
  7. fallback به yt-dlp روی URL صفحه

وابستگی‌ها:
    pip install aiohttp aiofiles curl_cffi yt-dlp
"""

import asyncio
import base64
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

logger = logging.getLogger("SxyprnHandler")

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

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MIN_VALID_VIDEO_SIZE = 100 * 1024  # 100 KB
PROGRESS_INTERVAL = 1.0
CHUNK_SIZE = 1024 * 1024  # 1 MB (single connection)
MAX_RETRIES = 3
RETRY_DELAY = 2.0
MULTI_SEGMENT_MIN_SIZE = 5 * 1024 * 1024  # 5 MB

MULTI_SEGMENT_WORKERS = 32
MULTI_SEGMENT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB
CONNECTOR_LIMIT = 50
CONNECTOR_LIMIT_PER_HOST = 50

_ALLOWED_HOSTS = frozenset({
    "sxyprn.net",
    "www.sxyprn.net",
    "sxyprn.com",
    "www.sxyprn.com",
    "sxyprn.org",
    "www.sxyprn.org",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_sxyprn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به sxyprn هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".sxyprn.")
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


def _ensure_https_referer(referer: str) -> str:
    """تبدیل referer به https — sxyprn CDN فقط https accept می‌کنه."""
    if not referer:
        return "https://sxyprn.net/"
    if referer.startswith("http://"):
        return "https://" + referer[len("http://"):]
    return referer


# ─── URL Transformation (getvsrc from main2.js) ────────────────────────────


def _ssut51(arg: str) -> int:
    """JavaScript: ssut51 — sum of all digits in arg."""
    s = re.sub(r'[^0-9]', '', arg)
    return sum(int(c) for c in s) if s else 0


def _boo(ss: int, es: int, host: str = "sxyprn.net") -> str:
    """JavaScript: boo — base64(ss + "-" + host + "-" + es) with +/= replaced."""
    raw = f"{ss}-{host}-{es}"
    encoded = base64.b64encode(raw.encode()).decode()
    return encoded.replace('+', '-').replace('/', '_').replace('=', '.')


def _preda(arg: List[str]) -> List[str]:
    """JavaScript: preda — arg[5] -= ssut51(arg[6]) + ssut51(arg[7])."""
    try:
        arg[5] = str(int(arg[5]) - _ssut51(arg[6]) - _ssut51(arg[7]))
    except (ValueError, IndexError):
        pass
    return arg


def _transform_video_url(src: str, host: str = "sxyprn.net") -> str:
    """
    اعمال transformation تابع getvsrc از main2.js.

    ورودی: /cdn/c5/HASH1/HASH2/EXPIRY/AUTHOR_ID/FILENAME.vid
    خروجی: /cdn5/{base64}/c5/HASH1/HASH2/{EXPIRY - digit_sum(AUTHOR) - digit_sum(FILENAME)}/AUTHOR_ID/FILENAME.vid
    """
    tmp = src.split("/")
    # tmp = ["", "cdn", "c5", "HASH1", "HASH2", "EXPIRY", "AUTHOR_ID", "FILENAME.vid"]
    # indices: 0=empty, 1=cdn, 2=c5, 3=HASH1, 4=HASH2, 5=EXPIRY, 6=AUTHOR_ID, 7=FILENAME

    if len(tmp) < 8:
        logger.error("Invalid src format: %s", src)
        return src

    # tmp[1] += "5" + "/" + boo(ssut51(tmp[6]), ssut51(tmp[7]))
    boo_val = _boo(_ssut51(tmp[6]), _ssut51(tmp[7]), host)
    tmp[1] = tmp[1] + "5" + "/" + boo_val

    # tmp = preda(tmp)
    tmp = _preda(tmp)

    # join
    return "/".join(tmp)


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
        title = re.sub(r"\s*[-|@]\s*(?:sxyprn\.net|sxyprn\.com|Sxyprn)\s*$", "", title, flags=re.IGNORECASE)
        if title:
            return title

    # روش 2: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|@]\s*(?:sxyprn\.net|sxyprn\.com|Sxyprn)\s*$", "", title, flags=re.IGNORECASE)
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
    # fallback: poster attribute
    m = re.search(r'<video[^>]*poster=[\'"]([^\'"]+)[\'"]', html, re.IGNORECASE)
    if m:
        url = m.group(1).strip()
        if url.startswith("//"):
            url = "https:" + url
        return url
    return ""


def _extract_duration(html: str) -> Optional[int]:
    """استخراج مدت زمان از meta یا Video Info text."""
    # از meta duration
    m = re.search(r'itemprop=["\']duration["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
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

    # از Video Info text: "duration:18:40"
    m = re.search(r'duration:<b>(\d+):(\d+)</b>', html, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))

    return None


def _extract_video_info(html: str) -> dict:
    """استخراج info از Video Info text."""
    info = {}
    # pattern: "duration:<b>18:40</b> · resolution:<b>HD</b>720 · bitrate:<b>1312</b> kb/s · size:<b>175</b> MB"
    m = re.search(
        r'duration:<b>([^<]+)</b>\s*·\s*resolution:<b>([^<]+)</b>(\d*)\s*·\s*bitrate:<b>(\d+)</b>\s*kb/s\s*·\s*size:<b>(\d+)</b>\s*MB',
        html, re.IGNORECASE,
    )
    if m:
        info["duration_str"] = m.group(1).strip()
        info["resolution_label"] = m.group(2).strip()  # HD or SD
        info["height"] = m.group(3).strip()
        info["bitrate"] = int(m.group(4))
        info["size_mb"] = int(m.group(5))
        info["is_hd"] = "HD" in info["resolution_label"].upper()
    return info


def _extract_vidsnfo(html: str) -> dict:
    """
    استخراج data-vnfo از HTML.

    ساختار:
      <span class='vidsnfo' data-vnfo='{"POST_ID":"/cdn/c5/.../filename.vid"}'></span>

    نکته: data-vnfo ممکنه با single quotes یا HTML entities باشه.
    """
    # پیدا کردن تگ vidsnfo با data-vnfo
    # ساختار: <span class='vidsnfo' data-vnfo='{"POST_ID":"/cdn/c5/.../filename.vid"}'></span>
    # نکته: data-vnfo با single quote ' احاطه شده و JSON داخلش double quote داره
    # پس باید تا single quote بعدی بخونیم

    # الگوی اصلی: data-vnfo='...' (با single quote)
    m = re.search(r"data-vnfo='([^']+)'", html, re.IGNORECASE)
    if not m:
        # الگوی alt: data-vnfo="..." (با double quote)
        m = re.search(r'data-vnfo="([^"]+)"', html, re.IGNORECASE)
        if not m:
            logger.error("vidsnfo data-vnfo not found in HTML")
            return {}

    raw = m.group(1)
    # HTML entity decode
    raw = raw.replace("&quot;", '"').replace("&#039;", "'").replace("&apos;", "'").replace("&#x27;", "'")
    # JSON parse
    try:
        data = json.loads(raw)
        logger.info("vidsnfo parsed: %d posts", len(data))
        return data
    except json.JSONDecodeError as e:
        logger.warning(f"vidsnfo JSON parse failed: {e}")
        logger.debug(f"raw: {raw[:300]}")

    # fallback: regex
    try:
        result = {}
        pair_pattern = re.compile(r'''['"]([^'"]+)['"]\s*:\s*['"]([^'"]+)['"]''')
        for match in pair_pattern.finditer(raw):
            key = match.group(1)
            value = match.group(2).replace("\\/", "/")
            result[key] = value
        if result:
            logger.info("vidsnfo parsed via regex: %d posts", len(result))
            return result
    except Exception as e:
        logger.debug(f"regex fallback failed: {e}")

    logger.error("vidsnfo parse failed completely")
    return {}


def _extract_video_sources(html: str, page_url: str) -> List[dict]:
    """
    استخراج URL ویدیو از HTML با اعمال transformation.

    Returns:
        list of dicts: [{label, url, height, quality_key, method, is_hd}, ...]
    """
    sources = []

    # host از page_url
    parsed = urlparse(page_url)
    host = parsed.hostname or "sxyprn.net"
    # host بدون www
    if host.startswith("www."):
        host = host[4:]

    vidsnfo = _extract_vidsnfo(html)
    if not vidsnfo:
        logger.error("No vidsnfo found")
        return []

    video_info = _extract_video_info(html)

    for post_id, encoded_path in vidsnfo.items():
        # اعمال transformation
        transformed_path = _transform_video_url(encoded_path, host=host)
        # تبدیل به URL کامل
        if transformed_path.startswith("//"):
            video_url = "https:" + transformed_path
        elif transformed_path.startswith("/"):
            video_url = f"https://{host}{transformed_path}"
        else:
            video_url = transformed_path

        logger.info("Transformed video URL: %s", video_url[:120])

        # کیفیت از video_info
        height_str = video_info.get("height", "")
        is_hd = video_info.get("is_hd", True)
        try:
            height = int(height_str) if height_str else (720 if is_hd else 480)
        except ValueError:
            height = 720 if is_hd else 480

        quality_text = f"{height}p"
        label = f"📺 MP4 {quality_text}" + (" HD" if is_hd else "")

        sources.append({
            "label": label,
            "url": video_url,
            "height": height,
            "quality_key": quality_text.lower(),
            "method": "vidsnfo_transform",
            "is_hd": is_hd,
            "post_id": post_id,
            "video_info": video_info,
        })

    return sources


# ─── Fetch Page ───────────────────────────────────────────────────────────


async def _fetch_page(url, jar=None, method="aiohttp"):
    """fetch صفحه با fallback."""
    headers = dict(_DEFAULT_HEADERS)

    # روش 1: curl_cffi (preferred — برای سایت‌های پشت CF)
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
                    if "vidsnfo" in text or "data-vnfo" in text:
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
                        logger.warning("curl_cffi: 200 ولی vidsnfo پیدا نشد")
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
                        if "vidsnfo" in html or "data-vnfo" in html:
                            logger.info("Page fetched via aiohttp: %s (size=%d)", url[:80], len(html))
                            return html, local_jar, ""
                        else:
                            logger.warning("aiohttp: 200 ولی vidsnfo پیدا نشد")
                    logger.warning("aiohttp fetch: HTTP %d", resp.status)
        except Exception as e:
            logger.warning(f"aiohttp fetch error: {e}")

    return None, jar, "Failed to fetch page"


# ─── Main API: extract qualities ──────────────────────────────────────────


async def extract_sxyprn_qualities(url, progress_cb=None):
    """استخراج کیفیت‌های ویدیو."""
    if not is_sxyprn_url(url):
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
    sources = _extract_video_sources(html, page_url=url)

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
        "fetch_method": "aiohttp" if not _check_curl_cffi() else "curl_cffi",
        "html_size": len(html),
        "video_info": sources[0].get("video_info", {}) if sources else {},
    }


# ─── Download: Multi-segment (fast) ───────────────────────────────────────


active_downloads: dict = {}


async def _download_multi_segment(
    direct_url, filepath, referer, cookies, progress_cb, dl_id="",
    num_workers=MULTI_SEGMENT_WORKERS,
):
    """دانلود چند تیکه‌ای با work-queue pattern.

    نکته مهم: sxyprn CDN از TLS fingerprinting استفاده می‌کنه.
    باید از curl_cffi با impersonate=chrome استفاده کنیم.
    aiohttp با TLS fingerprint پایتون 403/404 می‌گیره.
    """
    if not _check_curl_cffi():
        return False, "curl_cffi not installed (required for sxyprn)", 0

    from curl_cffi.requests import AsyncSession

    try:
        # هدر برای CDN — فقط User-Agent و Referer (curl_cffi بقیه رو مدیریت می‌کنه)
        cdn_headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": referer,
        }

        content_length = 0
        accept_ranges = ""

        # نکته مهم: sxyprn CDN HEAD request رو 404 می‌ده.
        # باید مستقیم با GET + Range probe کنیم.
        # نکته ۲: Range باید بزرگتر از 1 بایت باشه (bytes=0-0 کار نمی‌کنه).
        # نکته ۳: URL باید با همون session ای استفاده بشه که probe شده.
        # اگه session جدید بسازیم، URL ممکنه expire بشه.

        # session اشتراکی برای probe و chunks
        shared_session = AsyncSession()

        # ─── 1. probe با Range ───
        try:
            probe_headers = dict(cdn_headers)
            probe_headers["Range"] = "bytes=0-1023"
            resp = await shared_session.get(
                direct_url, impersonate="chrome",
                headers=probe_headers, allow_redirects=True, timeout=15,
            )
            logger.info(
                "[DL-SP] Probe: status=%d, content-length=%s, content-type=%s, content-range=%s",
                resp.status_code,
                resp.headers.get("Content-Length"),
                resp.headers.get("Content-Type"),
                resp.headers.get("Content-Range"),
            )
            if resp.status_code in (200, 206):
                if resp.status_code == 206:
                    accept_ranges = "bytes"
                    cr = resp.headers.get("Content-Range", "")
                    m = re.search(r"/(\d+)", cr)
                    if m:
                        content_length = int(m.group(1))
                else:
                    content_length = int(resp.headers.get("Content-Length", 0))
            elif resp.status_code == 403:
                await shared_session.close()
                return False, "HTTP_403", 0
            elif resp.status_code == 410:
                await shared_session.close()
                return False, "URL_EXPIRED", 0
            elif resp.status_code == 404:
                await shared_session.close()
                return False, "HTTP_404", 0
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
            "[DL-SP] Work-queue: %d chunks, %d workers, total=%d",
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
        # (shared_session در بالا ساخته شده برای probe)
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
                        resp = await shared_session.get(
                            direct_url,
                            impersonate="chrome",
                            headers={**cdn_headers, "Range": f"bytes={byte_start}-{byte_end}"},
                            allow_redirects=True,
                            timeout=120,
                        )
                        try:
                            if resp.status_code not in (200, 206):
                                raise Exception(f"HTTP {resp.status_code}")
                            chunk_data = bytearray(resp.content)
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
                        finally:
                            try:
                                resp.close()
                            except Exception:
                                pass
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(
                            "[DL-SP] W%d c%d attempt %d failed: %s",
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
                    "[DL-SP] %d worker failures, %d chunk failures",
                    len(real_failures), len(failed_chunks),
                )
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(real_failures)+len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-SP] Work-queue error: {e}", exc_info=True)
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
            "[DL-SP] Multi-segment DONE | size=%s | time=%.1fs | avg_speed=%.1f MB/s",
            _format_size(file_size), elapsed, avg_speed,
        )
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-SP] Multi-segment error: {e}", exc_info=True)
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
        "Sec-Fetch-Site": "same-origin",
        "Referer": referer,
    }

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
            "--add-header", "Referer:https://sxyprn.net/",
            "--force-generic-extractor",
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
        logger.info(f"[DL-SP] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-SP] yt-dlp error: {e}", exc_info=True)
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


async def download_sxyprn_video(
    page_url, video_url, filepath, progress_cb=None, cookies=None, dl_id="",
    quality_key="",
):
    """دانلود ویدیو از sxyprn با URL transform شده."""
    if not is_sxyprn_url(page_url):
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
    logger.info(f"[DL-SP] Attempt 1: multi-segment ({MULTI_SEGMENT_WORKERS} workers)")
    success, error, size = await _download_multi_segment(
        video_url, filepath, _ensure_https_referer(referer), cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0

    # refresh URL اگه 403/410
    if error in ("HTTP_403", "URL_EXPIRED"):
        logger.info(f"[DL-SP] {error}, refreshing session...")
        if progress_cb:
            await progress_cb("🔄 **Refreshing session...**")
        try:
            new_sources, _, new_info = await extract_sxyprn_qualities(page_url, progress_cb=None)
            if new_sources:
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
                logger.info("[DL-SP] Got fresh URL")
        except Exception as e:
            logger.warning(f"[DL-SP] refresh failed: {e}")

        success, error, size = await _download_multi_segment(
            video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
        )
        if success:
            return True, "", size
    logger.info(f"[DL-SP] Multi-segment failed: {error}")
    _cleanup_file(filepath)

    # ── روش 2: single-connection ──
    logger.info("[DL-SP] Attempt 2: single-connection")
    success, error, size = await _download_single_aiohttp(
        video_url, filepath, _ensure_https_referer(referer), cookies, progress_cb, dl_id=dl_id,
    )
    if success:
        return True, "", size
    logger.info(f"[DL-SP] Single failed: {error}")
    _cleanup_file(filepath)

    # ─ـ روش 3: yt-dlp ──
    logger.info("[DL-SP] Attempt 3: yt-dlp")
    success, error, size = await _download_with_ytdlp(
        page_url, filepath, progress_cb, quality_key=quality_key,
    )
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


async def download_sxyprn_direct(
    url, filepath, progress_cb=None, video_url="", quality="high", dl_id="",
):
    """Wrapper برای سازگاری با bot architecture."""
    if not video_url:
        qualities, title, info = await extract_sxyprn_qualities(url, progress_cb)
        if not qualities:
            return False, title or "Extraction failed", 0
        selected = qualities[0]  # sxyprn فقط یه کیفیت داره
        video_url = selected["url"]
        quality_key = selected.get("quality_key", "")
        cookies = info.get("cookies", {})
    else:
        qualities, title, info = await extract_sxyprn_qualities(url, progress_cb)
        cookies = info.get("cookies", {}) if info else {}
        quality_key = quality

    return await download_sxyprn_video(
        url, video_url, filepath, progress_cb,
        cookies=cookies, dl_id=dl_id, quality_key=quality_key,
    )


# ─── Self-test ─────────────────────────────────────────────────────────────


async def _self_test():
    """تست خودي هندلر."""
    test_url = "http://sxyprn.net/post/6a4fe33d3d0f1?sk=Sister&so=0&ss=latest"
    print(f"\n{'═' * 80}")
    print(f"Self-test: {test_url}")
    print(f"{'═' * 80}\n")

    async def progress(msg):
        print(f"  → {msg}")

    sources, title, info = await extract_sxyprn_qualities(test_url, progress_cb=progress)

    print(f"\n  Title: {title}")
    print(f"  Thumbnail: {info.get('thumbnail', '')[:120]}")
    print(f"  Duration: {info.get('duration', '?')}s")
    print(f"  Video Info: {info.get('video_info', {})}")
    print(f"\n  Sources ({len(sources)}):")
    for s in sources:
        print(f"    [{s['quality_key']:6s}] {s['url'][:120]}")
        print(f"             post_id={s.get('post_id')}")

    return sources, title, info


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_self_test())
