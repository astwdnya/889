"""
paradisehill_handler.py
──────────────────────
استخراج و دانلود ویدیو از en.paradisehill.cc

روش کار (بر اساس تحلیل واقعی صفحه):

  ─── ساختار صفحه ───
  صفحه فیلم HTML شامل یه inline JS که videoList تعریف می‌کنه:

    var videoList = [
      {"sources":[{"src":"https://v1.paradisehill.cc/video/HASH_part1.mp4","type":"video/mp4"}]},
      {"sources":[{"src":"https://v1.paradisehill.cc/video/HASH_part2.mp4","type":"video/mp4"}]},
      {"sources":[{"src":"https://v1.paradisehill.cc/video/HASH_part3.mp4","type":"video/mp4"}]}
    ];

  ─── نکته مهم: Multi-Part Movie ───
  paradisehill فیلم‌ها رو به چند part (معمولاً 1-3) تقسیم می‌کنه.
  هر part یه فایل MP4 جداگانه هست که باید جدا دانلود و (اختیاری) concat بشن.

  ─── ساختار URL ویدیو ───
  https://v1.paradisehill.cc/video/{HASH}_part{N}.mp4
    - HASH: hash منحصر‌به‌فرم فیلم (مثل 47a7565d04f9a_2k5uTaCiCj)
    - N: شماره part (1, 2, 3, ...)

  ─── سرور ───
  - en.paradisehill.cc: nginx/1.26.3 (بدون Cloudflare!)
  - v1.paradisehill.cc: nginx/1.14.2 (CDN با Range support)
  - بدون hotlink protection (همه referer ها کار می‌کنن)

  ─── کوکی ───
  - PHPSESSID, _csrf-frontend (لازم نیست برای CDN)

  ─── رفتار CDN ───
  - Accept-Ranges: bytes ✓ (multi-segment download کار می‌کنه)
  - Content-Type: video/mp4
  - بدون Rate limit (تست شده)

استراتژی دانلود:
  1. fetch صفحه paradisehill با aiohttp (سریع — بدون CF)
  2. fallback به curl_cffi با impersonate=chrome
  3. استخراج videoList JSON از HTML
  4. multi-segment download با 32 workers برای هر part
  5. fallback به single-connection
  6. fallback به yt-dlp روی URL صفحه

  نکته: اگه چند part داشته باشیم، هر part جدا دانلود می‌شه و می‌تونه
  با ffmpeg concat بشه (اختیاری). هندلر فعلی فقط part1 رو دانلود می‌کنه
  (یا part مشخص شده توسط parameter).

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

logger = logging.getLogger("ParadiseHillHandler")

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
    "paradisehill.cc",
    "en.paradisehill.cc",
    "www.paradisehill.cc",
    "ru.paradisehill.cc",
})

# CDN مجاز (paradisehill variants: v1, v2, v3 و...)
_ALLOWED_CDN_HOSTS = frozenset({
    "v1.paradisehill.cc",
    "v2.paradisehill.cc",
    "v3.paradisehill.cc",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_paradisehill_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به paradisehill هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".paradisehill.cc")
    except Exception:
        return False


def _is_cdn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به CDN مجاز هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_CDN_HOSTS or (
            host.endswith(".paradisehill.cc") and host.startswith("v")
        )
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
    """استخراج عنوان فیلم."""
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
        title = re.sub(r"\s*[-|@]\s*(?:paradisehill\.cc|ParadiseHill)\s*$", "", title, flags=re.IGNORECASE)
        if title:
            return title

    # روش 2: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        # remove "Porn Film Online - " prefix
        title = re.sub(r"^Porn Film Online\s*-\s*", "", title, flags=re.IGNORECASE)
        title = re.sub(r"\s*[-|@]\s*(?:paradisehill\.cc|ParadiseHill|Watching Free!)\s*$", "", title, flags=re.IGNORECASE)
        title = title.strip()
        if title:
            return title

    return "Untitled"


def _extract_thumbnail(html: str) -> str:
    m = re.search(
        r'(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if m:
        url = m.group(1).strip()
        if url.startswith("/"):
            url = "https://en.paradisehill.cc" + url
        elif url.startswith("//"):
            url = "https:" + url
        return url
    return ""


def _extract_duration(html: str) -> Optional[int]:
    """استخراج مدت زمان از JSON-LD یا meta."""
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


def _extract_video_list(html: str) -> List[dict]:
    """
    استخراج videoList JSON از HTML.

    ساختار:
      var videoList = [
        {"sources":[{"src":"https://...part1.mp4","type":"video/mp4"}]},
        {"sources":[{"src":"https://...part2.mp4","type":"video/mp4"}]},
        ...
      ];

    Returns:
        list of dicts: [{part_num, url, label}, ...]
    """
    parts = []

    # الگوی اصلی: var videoList = [...];
    m = re.search(
        r'var\s+videoList\s*=\s*(\[.*?\]);',
        html, re.DOTALL,
    )
    if not m:
        # الگوی alt بدون semicolon
        m = re.search(
            r'var\s+videoList\s*=\s*(\[.*?\])\s*</script>',
            html, re.DOTALL,
        )
        if not m:
            logger.error("videoList پیدا نشد در HTML")
            return []

    raw_json = m.group(1)
    # fix \\/ and \u0026
    cleaned = raw_json.replace("\\/", "/").replace("\\u0026", "&")

    try:
        video_list = json.loads(cleaned)
        logger.info("videoList parsed: %d parts", len(video_list))
    except json.JSONDecodeError as e:
        logger.error("videoList parse failed: %s", e)
        # fallback با regex
        return _extract_video_list_regex(html)

    for i, item in enumerate(video_list, 1):
        sources = item.get("sources", [])
        if not sources:
            continue
        # اولین source رو می‌گیریم (معمولاً فقط یه source هست)
        src = sources[0].get("src", "").strip()
        if not src:
            continue
        src = _clean_url(src)

        # protocol-relative
        if src.startswith("//"):
            src = "https:" + src

        # فقط CDN مجاز
        if not _is_cdn_url(src):
            logger.debug("Skipping non-CDN URL: %s", src[:80])
            continue

        parts.append({
            "part_num": i,
            "url": src,
            "label": f"Part {i}",
        })
        logger.info("Found part %d: %s", i, src[:100])

    return parts


def _extract_video_list_regex(html: str) -> List[dict]:
    """Fallback: استخراج با regex."""
    parts = []
    # الگو: {"src":"https://...partN.mp4","type":"video/mp4"}
    pattern = re.compile(
        r'\{\s*"src"\s*:\s*"(https?://[^"]+\.mp4)"\s*,\s*"type"\s*:\s*"video/mp4"\s*\}',
        re.IGNORECASE,
    )
    seen = set()
    for i, m in enumerate(pattern.finditer(html), 1):
        url = m.group(1).replace("\\/", "/").replace("\\u0026", "&")
        url = _clean_url(url)
        if url in seen:
            continue
        seen.add(url)
        if not _is_cdn_url(url):
            continue
        parts.append({
            "part_num": i,
            "url": url,
            "label": f"Part {i}",
        })
        logger.info("Found part %d via regex: %s", i, url[:100])

    return parts


def _extract_video_sources(html: str) -> List[dict]:
    """
    استخراج URL های ویدیو از HTML.

    Returns:
        list of dicts: [{label, url, height, quality_key, method, is_hd, part_num}, ...]
    """
    sources = []
    parts = _extract_video_list(html)

    if not parts:
        logger.error("No video parts found")
        return []

    for part in parts:
        # paradisehill کیفیت‌های مختلف نداره — فقط یه quality per part
        # اما برای سازگاری با API بقیه هندلرها، quality_key=hd می‌ذاریم
        sources.append({
            "label": f"📺 MP4 {part['label']}",
            "url": part["url"],
            "height": 720,  # paradisehill معمولاً 720p هست
            "quality_key": "720p",
            "method": "videoList",
            "is_hd": True,
            "part_num": part["part_num"],
        })

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
                        if "videoList" in html or "paradisehill.cc/video/" in html:
                            logger.info("Page fetched via aiohttp: %s (size=%d)", url[:80], len(html))
                            return html, local_jar, ""
                        else:
                            logger.warning("aiohttp: 200 ولی videoList پیدا نشد")
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
                    if "videoList" in text or "paradisehill.cc/video/" in text:
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
                        logger.warning("curl_cffi: 200 ولی videoList پیدا نشد")
                logger.warning("curl_cffi fetch: HTTP %d", resp.status_code)
        except Exception as e:
            logger.warning(f"curl_cffi fetch error: {e}")

    return None, jar, "Failed to fetch page"


# ─── Main API: extract qualities ──────────────────────────────────────────


async def extract_paradisehill_qualities(url, progress_cb=None):
    """استخراج کیفیت‌های ویدیو (parts)."""
    if not is_paradisehill_url(url):
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
    sources = _extract_video_sources(html)

    if not sources:
        logger.error("No video sources found in page")
        return [], "URL ویدیو در صفحه پیدا نشد", {}

    cookies = {}
    if jar:
        for cookie in jar:
            cookies[cookie.key] = cookie.value

    logger.info("Found %d video parts", len(sources))

    if progress_cb:
        labels = ", ".join(s["label"] for s in sources)
        dur_str = ""
        if duration:
            mins, secs = divmod(duration, 60)
            dur_str = f" ({mins}:{secs:02d})"
        await progress_cb(
            f"✅ **پیدا شد:** {title[:50]}{dur_str}\n"
            f"🎞 پارت‌ها: {labels}\n"
            f"⚠ این فیلم {len(sources)} پارت داره — part1 دانلود می‌شه"
        )

    return sources, title, {
        "thumbnail": thumbnail,
        "page_url": url,
        "cookies": cookies,
        "duration": duration,
        "fetch_method": "aiohttp",
        "html_size": len(html),
        "parts_count": len(sources),
    }


# ─── Download: Multi-segment (fast) ───────────────────────────────────────


active_downloads: dict = {}


async def _download_multi_segment(
    direct_url, filepath, referer, cookies, progress_cb, dl_id="",
    num_workers=MULTI_SEGMENT_WORKERS,
    part_label="",
):
    """دانلود چند تیکه‌ای با work-queue pattern — OPTIMIZED برای سرعت بالا."""
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
            except Exception as e:
                logger.warning(f"Probe request failed: {e}")

        if content_length == 0:
            return False, "Cannot determine file size", 0
        if content_length > MAX_DOWNLOAD_SIZE:
            return False, f"File too large: {_format_size(content_length)}", 0
        if accept_ranges != "bytes" or content_length < MULTI_SEGMENT_MIN_SIZE:
            return False, "Range not supported or file too small", 0

        total_mb = content_length / 1024 / 1024
        prefix = f"[{part_label}] " if part_label else ""
        await progress_cb(
            f"📥 {prefix}**Downloading...**\n"
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
            "[DL-PH] %sWork-queue: %d chunks, %d workers, total=%d",
            prefix, total_chunks, num_workers, content_length,
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
                    f"📥 {prefix}**Downloading...**\n`[{bar}]`\n"
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
                            "[DL-PH] %s W%d c%d attempt %d failed: %s",
                            prefix, worker_id, c_idx, attempt + 1, e,
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
                    "[DL-PH] %s %d worker failures, %d chunk failures",
                    prefix, len(real_failures), len(failed_chunks),
                )
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(real_failures)+len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-PH] Work-queue error: {e}", exc_info=True)
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
            "[DL-PH] %s Multi-segment DONE | size=%s | time=%.1fs | avg_speed=%.1f MB/s",
            prefix, _format_size(file_size), elapsed, avg_speed,
        )
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-PH] Multi-segment error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Download: Single connection (fallback) ───────────────────────────────


async def _download_single_aiohttp(url, filepath, referer, cookies, progress_cb, dl_id="", part_label=""):
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
    prefix = f"[{part_label}] " if part_label else ""
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
                                        prefix + _format_progress(downloaded, content_length, start_time, now)
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


async def _download_with_ytdlp(url, filepath, progress_cb, quality_key="", part_label=""):
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0
    has_curl_cffi = _check_curl_cffi()
    prefix = f"[{part_label}] " if part_label else ""
    await progress_cb(f"📥 {prefix}**Fallback: yt-dlp...**")
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
            "--add-header", "Referer:https://en.paradisehill.cc/",
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
                        await progress_cb(prefix + msg)
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
        logger.info(f"[DL-PH] {prefix}yt-dlp DONE | size={_format_size(size)}")
        return True, "", size
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-PH] yt-dlp error: {e}", exc_info=True)
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


async def download_paradisehill_video(
    page_url, video_url, filepath, progress_cb=None, cookies=None, dl_id="",
    quality_key="", part_num=1,
):
    """دانلود یه part از paradisehill."""
    if not is_paradisehill_url(page_url):
        return False, "URL host not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0
    if progress_cb is None:
        async def _noop(msg): pass
        progress_cb = _noop
    if dl_id and dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}
    referer = "https://en.paradisehill.cc/"
    if not cookies:
        cookies = {}

    part_label = f"Part {part_num}" if part_num > 0 else ""

    # ── روش 1: multi-segment ──
    logger.info(f"[DL-PH] {part_label} Attempt 1: multi-segment ({MULTI_SEGMENT_WORKERS} workers)")
    success, error, size = await _download_multi_segment(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
        part_label=part_label,
    )
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0
    if error == "HTTP_403":
        logger.info(f"[DL-PH] {part_label} 403, refreshing session...")
        if progress_cb:
            await progress_cb(f"🔄 {part_label} **Refreshing session...**")
        try:
            new_sources, _, new_info = await extract_paradisehill_qualities(page_url, progress_cb=None)
            if new_sources:
                # پیدا کردن part همون شماره
                new_video_url = None
                for q in new_sources:
                    if q.get("part_num") == part_num:
                        new_video_url = q["url"]
                        break
                if not new_video_url:
                    new_video_url = new_sources[0]["url"]
                video_url = new_video_url
                new_cookies = new_info.get("cookies", {})
                cookies.update(new_cookies)
                logger.info(f"[DL-PH] {part_label} Got fresh URL")
        except Exception as e:
            logger.warning(f"[DL-PH] {part_label} refresh failed: {e}")

        success, error, size = await _download_multi_segment(
            video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
            part_label=part_label,
        )
        if success:
            return True, "", size
    logger.info(f"[DL-PH] {part_label} Multi-segment failed: {error}")
    _cleanup_file(filepath)

    # ── روش 2: single-connection ──
    logger.info(f"[DL-PH] {part_label} Attempt 2: single-connection")
    success, error, size = await _download_single_aiohttp(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
        part_label=part_label,
    )
    if success:
        return True, "", size
    logger.info(f"[DL-PH] {part_label} Single failed: {error}")
    _cleanup_file(filepath)

    # ─ـ روش 3: yt-dlp ──
    logger.info(f"[DL-PH] {part_label} Attempt 3: yt-dlp")
    success, error, size = await _download_with_ytdlp(
        video_url, filepath, progress_cb, quality_key=quality_key,
        part_label=part_label,
    )
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


async def download_paradisehill_direct(
    url, filepath, progress_cb=None, video_url="", quality="high", dl_id="",
    part_num=1,
):
    """Wrapper برای سازگاری با bot architecture.

    نکته: paradisehill چند part داره. این wrapper فقط part مشخص شده رو دانلود می‌کنه.
    برای دانلود همه parts، باید extract_paradisehill_qualities رو صدا بزنید و
    برای هر part جدا download_paradisehill_video رو فراخوانی کنید.
    """
    if not video_url:
        qualities, title, info = await extract_paradisehill_qualities(url, progress_cb)
        if not qualities:
            return False, title or "Extraction failed", 0
        # پیدا کردن part مورد نظر
        selected = None
        for q in qualities:
            if q.get("part_num") == part_num:
                selected = q
                break
        if not selected:
            # fallback به part 1
            selected = qualities[0]
            part_num = selected.get("part_num", 1)
        video_url = selected["url"]
        quality_key = selected.get("quality_key", "")
        cookies = info.get("cookies", {})
    else:
        qualities, title, info = await extract_paradisehill_qualities(url, progress_cb)
        cookies = info.get("cookies", {}) if info else {}
        quality_key = quality

    return await download_paradisehill_video(
        url, video_url, filepath, progress_cb,
        cookies=cookies, dl_id=dl_id, quality_key=quality_key,
        part_num=part_num,
    )


# ─── Helper: download all parts ────────────────────────────────────────────


async def download_all_parts(
    url, output_dir, progress_cb=None, dl_id="", quality="high",
):
    """
    دانلود همه parts از یه فیلم paradisehill.

    هر part با نام part_{N}.mp4 در output_dir ذخیره می‌شه.

    Returns:
        list of dicts: [{part_num, success, filepath, size, error}, ...]
    """
    qualities, title, info = await extract_paradisehill_qualities(url, progress_cb)
    if not qualities:
        return []

    results = []
    os.makedirs(output_dir, exist_ok=True)
    # sanitize title for filename
    safe_title = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_')[:50]

    for q in qualities:
        part_num = q["part_num"]
        part_filepath = os.path.join(output_dir, f"{safe_title}_part{part_num}.mp4")

        if progress_cb:
            await progress_cb(f"🎬 **شروع دانلود Part {part_num}**")

        success, error, size = await download_paradisehill_video(
            url, q["url"], part_filepath, progress_cb,
            cookies=info.get("cookies", {}), dl_id=dl_id,
            quality_key=q.get("quality_key", ""), part_num=part_num,
        )

        results.append({
            "part_num": part_num,
            "success": success,
            "filepath": part_filepath if success else None,
            "size": size,
            "error": error,
        })

        if not success:
            logger.error(f"Part {part_num} failed: {error}")
            break  # اگه یه part fail شد، ادامه نمی‌دیم

    return results


# ─── Self-test ─────────────────────────────────────────────────────────────


async def _self_test():
    """تست خودي هندلر."""
    test_url = "https://en.paradisehill.cc/66fea23fa3ffc/"
    print(f"\n{'═' * 80}")
    print(f"Self-test: {test_url}")
    print(f"{'═' * 80}\n")

    async def progress(msg):
        print(f"  → {msg}")

    sources, title, info = await extract_paradisehill_qualities(test_url, progress_cb=progress)

    print(f"\n  Title: {title}")
    print(f"  Thumbnail: {info.get('thumbnail', '')[:120]}")
    print(f"  Duration: {info.get('duration', '?')}s")
    print(f"  Parts count: {info.get('parts_count', '?')}")
    print(f"\n  Sources ({len(sources)}):")
    for s in sources:
        print(f"    [{s.get('part_num', '?')}] {s['url'][:120]} ({s['method']})")

    return sources, title, info


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_self_test())
