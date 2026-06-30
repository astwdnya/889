"""
eporner_handler.py
------------------
استخراج لینک‌های دانلود از eporner.com و ارسال ویدیو به کاربر.

روش کار:
  - eporner از Video.js player استفاده میکنه
  - لینک‌های دانلود مستقیم (/dload/) توی HTML هستن
  - yt-dlp هم extractor مخصوص eporner داره
  - XHR API هم هست ولی نیاز به hash صحیح داره
  - فرمت‌ها: h264 + AV1 (240p تا 1080p)
  - CDN: vid-*.eporner.com
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("EpornerHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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

# ─── Constants ──────────────────────────────────────────────

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_RETRIES = 3
RETRY_DELAY = 2.0
PROGRESS_INTERVAL = 2.0
YTDLP_TIMEOUT = 90
DOWNLOAD_READ_TIMEOUT = 120

_ALLOWED_HOSTS = frozenset({"eporner.com", "www.eporner.com"})

_ALLOWED_CDN_SUFFIXES = (
    ".eporner.com",
    "-cdn.eporner.com",
    ".cdn.eporner.com",
)

_IMPERSONATE_TARGETS = [
    "chrome",
    "chrome:120",
    "chrome:110",
    "edge",
    "safari",
]

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_eporner_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به eporner هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".eporner.com")
    except Exception:
        return False


def _extract_video_id(url: str) -> Optional[str]:
    """استخراج video ID از URL."""
    m = re.search(r"/video-([a-zA-Z0-9]+)/", url)
    return m.group(1) if m else None


def _is_allowed_host(url: str) -> bool:
    """بررسی اینکه URL به دامنه‌های مجاز اشاره میکنه."""
    try:
        host = urlparse(url).hostname or ""
        return (
            host in _ALLOWED_HOSTS
            or any(host.endswith(s) for s in _ALLOWED_CDN_SUFFIXES)
        )
    except Exception:
        return False


def _is_video_cdn_url(url: str) -> bool:
    """بررسی اینکه URL یه لینک ویدیو CDN هست."""
    if _is_allowed_host(url):
        return True
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if parsed.scheme == "https" and (
            path.endswith(".mp4")
            or path.endswith(".m3u8")
            or "/get_file/" in path
            or "/dload/" in path
        ):
            return True
    except Exception:
        pass
    return False


def _normalize_url(url: str, base_url: str = "") -> Optional[str]:
    """نرمال‌سازی URL."""
    url = url.replace("\\/", "/").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http") and base_url:
        url = urljoin(base_url, url)
    elif not url.startswith("http"):
        return None
    return url


def _quality_sort_key(q: dict) -> int:
    """کلید مرتب‌سازی بر اساس عدد کیفیت."""
    nums = re.findall(r"\d+", q["label"])
    return int(nums[-1]) if nums else 0


def _cleanup_file(filepath: str) -> None:
    """حذف فایل اگه وجود داشته باشه."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Cleaned up file: %s", filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _format_progress(
    downloaded: int, content_length: int, start_time: float, now: float
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
        return (
            f"📥 **Downloading...**\n`[{bar}]`\n"
            f"💾 {dl_mb:.1f}/{total_mb:.1f} MB"
            f"  •  ⚡ {speed / 1024 / 1024:.1f} MB/s\n📊 {pct:.1f}%"
        )
    return (
        f"📥 **Downloading...**\n"
        f"💾 {dl_mb:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
    )


def _check_impersonation_support() -> bool:
    """بررسی اینکه curl_cffi نصبه."""
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


def _find_output_file(filepath: str) -> Optional[str]:
    """پیدا کردن فایل خروجی yt-dlp."""
    if os.path.exists(filepath):
        return filepath
    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = base + ext
        if os.path.exists(candidate):
            try:
                os.rename(candidate, filepath)
                return filepath
            except OSError:
                return candidate
    return None


# ─── HTTP helpers ───────────────────────────────────────────


@asynccontextmanager
async def _get_session(timeout: Optional[ClientTimeout] = None):
    """ساخت aiohttp session."""
    t = timeout or ClientTimeout(total=30, connect=10)
    jar = aiohttp.CookieJar(unsafe=True)
    session = aiohttp.ClientSession(
        timeout=t, headers=_DEFAULT_HEADERS, cookie_jar=jar
    )
    try:
        yield session
    finally:
        await session.close()


async def _fetch_with_retry(
    url: str,
    headers: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
    timeout: Optional[ClientTimeout] = None,
) -> Tuple[Optional[str], int]:
    """دریافت URL با retry."""
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            async with _get_session(timeout) as session:
                merged = {**_DEFAULT_HEADERS, **(headers or {})}
                async with session.get(
                    url, headers=merged, allow_redirects=True
                ) as resp:
                    if resp.status == 200:
                        return await resp.text(errors="replace"), 200
                    last_error = f"HTTP {resp.status}"
                    if 400 <= resp.status < 500:
                        return None, resp.status
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = str(e)[:200]
            logger.warning(
                "Attempt %d/%d failed for %s: %s",
                attempt, max_retries, url, last_error,
            )
        if attempt < max_retries:
            await asyncio.sleep(RETRY_DELAY * attempt)
    return None, 0


# ─── yt-dlp extraction ─────────────────────────────────────


async def _extract_with_ytdlp(url: str) -> Tuple[List[dict], str]:
    """استخراج لینک‌های ویدیو با yt-dlp (extractor مخصوص eporner داره)."""
    if not shutil.which("yt-dlp"):
        logger.error("yt-dlp is not installed")
        return [], "yt-dlp not installed"

    has_impersonation = _check_impersonation_support()

    attempts = []
    if has_impersonation:
        for target in _IMPERSONATE_TARGETS:
            attempts.append(("impersonate", target))
    attempts.append(("basic", None))

    error = ""
    for method, target in attempts:
        qualities, title, error = await _try_ytdlp_extract(url, method, target)
        if qualities:
            return qualities, title
        if "HTTP Error 403" not in error and "Cloudflare" not in error:
            break

    return [], error if error else "Extraction failed"


async def _try_ytdlp_extract(
    url: str, method: str, target: Optional[str]
) -> Tuple[List[dict], str, str]:
    """یک تلاش استخراج با yt-dlp."""
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-download",
        "--dump-json",
        "--no-check-certificates",
    ]

    if method == "impersonate" and target:
        cmd.extend(["--impersonate", target])
        logger.info("yt-dlp: trying --impersonate %s for eporner", target)
    else:
        logger.info("yt-dlp: trying basic for eporner")

    cmd.append(url)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=YTDLP_TIMEOUT
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return [], "", "yt-dlp timed out"

        if process.returncode != 0:
            err = stderr.decode(errors="replace")[:300]
            logger.debug("yt-dlp failed (%s/%s): %s", method, target, err)
            return [], "", err

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return [], "", "Empty output"

        first_line = raw.split("\n")[0]
        data = json.loads(first_line)

        title = data.get("title", "Untitled")
        qualities = _parse_ytdlp_formats(data)

        return qualities, title, ""

    except json.JSONDecodeError as e:
        return [], "", f"Invalid JSON: {e}"
    except Exception as e:
        return [], "", str(e)[:200]


def _parse_ytdlp_formats(data: dict) -> List[dict]:
    """پردازش فرمت‌های yt-dlp."""
    qualities: List[dict] = []
    seen_urls = set()

    formats = data.get("formats", [])
    if not formats:
        direct_url = data.get("url", "")
        if direct_url:
            ext = data.get("ext", "mp4")
            height = data.get("height")
            label = f"🎥 {ext.upper()} {height}p" if height else f"🎥 {ext.upper()}"
            qualities.append({
                "label": label,
                "url": direct_url,
                "method": "direct",
            })
        return qualities

    for fmt in formats:
        fmt_url = fmt.get("url", "")
        if not fmt_url or fmt_url in seen_urls:
            continue
        seen_urls.add(fmt_url)

        ext = fmt.get("ext", "mp4")
        height = fmt.get("height")
        vcodec = fmt.get("vcodec", "")
        protocol = fmt.get("protocol", "")
        format_id = fmt.get("format_id", "")
        filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0

        if vcodec == "none":
            continue

        is_m3u8 = (
            protocol in ("m3u8", "m3u8_native")
            or ".m3u8" in fmt_url
            or ext == "m3u8"
        )

        size_str = f" ({filesize / 1024 / 1024:.0f}MB)" if filesize else ""

        # تشخیص AV1
        is_av1 = "av1" in format_id.lower() or vcodec.startswith("av01")
        codec_tag = " AV1" if is_av1 else ""

        if height:
            prefix = "📡 M3U8" if is_m3u8 else f"🎥 MP4"
            label = f"{prefix} {height}p{codec_tag}{size_str}"
        elif format_id:
            label = f"🎥 {format_id}{size_str}"
        else:
            label = f"🎥 {ext.upper()}{size_str}"

        qualities.append({
            "label": label,
            "url": fmt_url,
            "method": "m3u8" if is_m3u8 else "direct",
        })

    return qualities


# ─── HTML extraction ───────────────────────────────────────


async def _extract_from_html(url: str) -> Tuple[List[dict], str]:
    """
    استخراج لینک‌های دانلود از HTML صفحه.
    eporner لینک‌های /dload/ رو مستقیم توی HTML داره.
    """
    html = None

    # اول curl_cffi
    if _check_impersonation_support():
        html = await _fetch_html_curl_cffi(url)

    # بعد aiohttp
    if not html:
        text, status = await _fetch_with_retry(url)
        html = text

    if not html:
        return [], "Could not fetch page"

    title = _extract_title(html)
    qualities: List[dict] = []

    # روش 1: لینک‌های /dload/ (مستقیم‌ترین روش)
    _extract_dload_links(html, url, qualities)

    # روش 2: JSON-LD contentUrl
    _extract_json_ld(html, url, qualities)

    # روش 3: video tag data-vid
    _extract_video_tag(html, url, qualities)

    # روش 4: XHR API با hash
    if not qualities:
        video_id = _extract_video_id(url)
        hash_val = _extract_hash(html)
        if video_id and hash_val:
            api_qualities = await _extract_from_xhr_api(video_id, hash_val)
            qualities.extend(api_qualities)

    return qualities, title


async def _fetch_html_curl_cffi(url: str) -> Optional[str]:
    """دریافت HTML با curl_cffi."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None

    try:
        async with AsyncSession() as session:
            resp = await session.get(
                url,
                impersonate="chrome",
                headers={
                    "Referer": "https://www.eporner.com/",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                allow_redirects=True,
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.text
            logger.debug("curl_cffi fetch: HTTP %d", resp.status_code)
    except Exception as e:
        logger.debug("curl_cffi fetch failed: %s", e)

    return None


def _extract_title(html: str) -> str:
    """استخراج عنوان ویدیو."""
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
        # حذف " - EPORNER" از آخر
        title = re.sub(r"\s*-\s*EPORNER\s*$", "", title, flags=re.IGNORECASE)
        # decode HTML entities
        title = title.replace("&#039;", "'").replace("&amp;", "&").replace("&quot;", '"')
        return title

    m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*-\s*EPORNER\s*$", "", title, flags=re.IGNORECASE)
        title = title.replace("&#039;", "'").replace("&amp;", "&")
        return title or "Untitled"

    return "Untitled"


def _extract_hash(html: str) -> Optional[str]:
    """استخراج hash از HTML برای XHR API."""
    m = re.search(r"""hash\s*[:=]\s*['"]([a-f0-9]{32})['"]""", html)
    return m.group(1) if m else None


def _extract_dload_links(html: str, page_url: str, qualities: List[dict]) -> None:
    """
    استخراج لینک‌های /dload/ از HTML.
    فرمت: /dload/{videoId}/{quality}/{filename}.mp4
    """
    base_url = "https://www.eporner.com"

    # پیدا کردن همه لینک‌های dload
    dload_pattern = r'["\'](/dload/[^"\']+\.mp4)["\']'
    matches = re.findall(dload_pattern, html)

    seen = set()
    for path in matches:
        if path in seen:
            continue
        seen.add(path)

        full_url = base_url + path

        # استخراج کیفیت از path
        # فرمت: /dload/CffKb9Bzweb/720/15595505-720p.mp4
        res_m = re.search(r"/(\d{3,4})/", path)
        is_av1 = "-av1" in path.lower()

        if res_m:
            quality = res_m.group(1)
            codec_tag = " AV1" if is_av1 else ""
            label = f"🎥 MP4 {quality}p{codec_tag}"
        else:
            label = "🎥 MP4 (AV1)" if is_av1 else "🎥 MP4"

        if any(q["url"] == full_url for q in qualities):
            continue

        qualities.append({
            "label": label,
            "url": full_url,
            "method": "direct",
        })


def _extract_json_ld(html: str, page_url: str, qualities: List[dict]) -> None:
    """استخراج از JSON-LD."""
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue

        if not isinstance(data, dict):
            continue

        content_url = data.get("contentUrl", "")
        if not content_url:
            continue

        video_url = _normalize_url(content_url, page_url)
        if not video_url:
            continue

        if any(q["url"] == video_url for q in qualities):
            continue

        qualities.append({
            "label": "🎥 MP4 (original)",
            "url": video_url,
            "method": "direct",
        })


def _extract_video_tag(html: str, page_url: str, qualities: List[dict]) -> None:
    """استخراج از video tag (data-vid attribute)."""
    m = re.search(r'data-vid=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        vid_path = m.group(1)
        # data-vid="CffKb9Bzweb/CffKb9Bzweb.mp4"
        # این فقط path هست، باید CDN URL ساخته بشه
        # ولی CDN URL نیاز به token داره، پس فقط لاگ میکنیم
        logger.debug("Found data-vid: %s", vid_path)


async def _extract_from_xhr_api(
    video_id: str, hash_val: str
) -> List[dict]:
    """
    استخراج از XHR API.
    endpoint: /xhr/video/{id}?hash={hash}
    """
    api_url = f"https://www.eporner.com/xhr/video/{video_id}?hash={hash_val}"

    headers = {
        **_DEFAULT_HEADERS,
        "Referer": f"https://www.eporner.com/video-{video_id}/",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    qualities: List[dict] = []

    try:
        text, status = await _fetch_with_retry(api_url, headers=headers)
        if not text or status != 200:
            return qualities

        data = json.loads(text)
        sources = data.get("sources", {})
        mp4_sources = sources.get("mp4", {})

        for quality_name, info in mp4_sources.items():
            if not isinstance(info, dict):
                continue
            src = info.get("src", "")
            if not src or "na.mp4" in src:
                continue

            # استخراج کیفیت
            res_m = re.search(r"(\d{3,4})p?", quality_name)
            is_av1 = "av1" in quality_name.lower()
            codec_tag = " AV1" if is_av1 else ""

            if res_m:
                label = f"🎥 MP4 {res_m.group(1)}p{codec_tag}"
            else:
                label = f"🎥 MP4 ({quality_name}){codec_tag}"

            if any(q["url"] == src for q in qualities):
                continue

            qualities.append({
                "label": label,
                "url": src,
                "method": "direct",
            })

    except Exception as e:
        logger.debug("XHR API extraction failed: %s", e)

    return qualities


# ─── Main extraction ───────────────────────────────────────


async def extract_eporner_qualities(
    url: str,
    debug_cb: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str]:
    """
    لینک‌های کیفیت مختلف رو از صفحه eporner استخراج میکنه.

    استراتژی:
      1. yt-dlp (extractor مخصوص eporner)
      2. HTML parsing (لینک‌های /dload/ + JSON-LD)
      3. XHR API
    """
    if not is_eporner_url(url):
        logger.warning("Not a valid eporner URL: %s", url)
        return [], "Invalid URL"

    # ── روش 1: yt-dlp (بهترین روش) ──
    if debug_cb:
        await debug_cb("🔍 Method 1: yt-dlp (Eporner extractor)...")
    logger.info("Method 1: yt-dlp for %s", url)
    qualities, title = await _extract_with_ytdlp(url)
    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        if debug_cb:
            await debug_cb(f"✅ Found {len(qualities)} qualities via yt-dlp")
        logger.info("Extracted %d qualities via yt-dlp", len(qualities))
        return qualities, title

    # ── روش 2: HTML parsing ──
    if debug_cb:
        await debug_cb("🔍 Method 2: HTML parsing...")
    logger.info("Method 2: HTML parsing for %s", url)
    qualities, title = await _extract_from_html(url)
    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        if debug_cb:
            await debug_cb(f"✅ Found {len(qualities)} qualities from HTML")
        logger.info("Extracted %d qualities from HTML", len(qualities))
        return qualities, title

    logger.warning("All extraction methods failed for: %s", url)

    has_ytdlp = shutil.which("yt-dlp") is not None
    if not has_ytdlp:
        return [], "yt-dlp is not installed. Install: pip install yt-dlp"

    return [], "Extraction failed - site may have updated its structure"


# ─── Download ──────────────────────────────────────────────


async def _download_multi_segment(
    url: str,
    filepath: str,
    referer: str,
    progress_cb: ProgressCallback,
    num_segments: int = 8,
) -> Tuple[bool, str, int]:
    """دانلود چند تیکه‌ای با چند connection همزمان."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        async with AsyncSession() as session:
            head_resp = await session.head(
                url,
                impersonate="chrome",
                headers={"Referer": referer},
                allow_redirects=True,
                timeout=15,
            )

            content_length = int(head_resp.headers.get("Content-Length", 0))
            accept_ranges = head_resp.headers.get("Accept-Ranges", "")

            if content_length == 0:
                return False, "Cannot determine file size", 0

            if content_length > MAX_DOWNLOAD_SIZE:
                return False, f"File too large: {content_length / 1024 / 1024:.0f} MB", 0

            if accept_ranges.lower() != "bytes":
                logger.info("Server doesn't support Range requests")
                return False, "Range not supported", 0

        total_mb = content_length / 1024 / 1024
        await progress_cb(
            f"📥 **دانلود چند تیکه‌ای ({num_segments} بخش)...**\n"
            f"💾 حجم: {total_mb:.1f} MB"
        )

        segment_size = content_length // num_segments
        segments = []
        for i in range(num_segments):
            start = i * segment_size
            end = content_length - 1 if i == num_segments - 1 else (i + 1) * segment_size - 1
            segments.append((i, start, end))

        segment_files = [f"{filepath}.part{i}" for i in range(num_segments)]
        downloaded_bytes = [0] * num_segments
        start_time = time.time()
        last_update = [0.0]
        lock = asyncio.Lock()

        async def _download_segment(seg_idx: int, byte_start: int, byte_end: int):
            seg_file = segment_files[seg_idx]
            for attempt in range(MAX_RETRIES):
                try:
                    async with AsyncSession() as seg_session:
                        resp = await seg_session.get(
                            url,
                            impersonate="chrome",
                            headers={
                                "Referer": referer,
                                "Range": f"bytes={byte_start}-{byte_end}",
                                "Accept": "*/*",
                            },
                            allow_redirects=True,
                            timeout=300,
                            stream=True,
                        )

                        if resp.status_code not in (200, 206):
                            raise Exception(f"HTTP {resp.status_code}")

                        async with aiofiles.open(seg_file, "wb") as f:
                            async for chunk in resp.aiter_content():
                                if not chunk:
                                    continue
                                await f.write(chunk)
                                downloaded_bytes[seg_idx] += len(chunk)

                                now = time.time()
                                async with lock:
                                    if now - last_update[0] >= PROGRESS_INTERVAL:
                                        last_update[0] = now
                                        total_dl = sum(downloaded_bytes)
                                        await progress_cb(
                                            _format_progress(total_dl, content_length, start_time, now)
                                        )
                        return

                except asyncio.CancelledError:
                    _cleanup_file(seg_file)
                    raise
                except Exception as e:
                    logger.warning("Segment %d attempt %d failed: %s", seg_idx, attempt + 1, e)
                    _cleanup_file(seg_file)
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))

            raise Exception(f"Segment {seg_idx} failed after {MAX_RETRIES} attempts")

        try:
            await asyncio.gather(
                *[_download_segment(idx, start, end) for idx, start, end in segments]
            )
        except Exception as e:
            for sf in segment_files:
                _cleanup_file(sf)
            return False, str(e)[:200], 0

        await progress_cb("🔗 **ترکیب بخش‌ها...**")
        try:
            async with aiofiles.open(filepath, "wb") as outfile:
                for sf in segment_files:
                    if not os.path.exists(sf):
                        raise FileNotFoundError(f"Missing segment: {sf}")
                    async with aiofiles.open(sf, "rb") as infile:
                        while True:
                            chunk = await infile.read(4 * 1024 * 1024)
                            if not chunk:
                                break
                            await outfile.write(chunk)
        finally:
            for sf in segment_files:
                _cleanup_file(sf)

        if not os.path.exists(filepath):
            return False, "Merged file not created", 0

        final_size = os.path.getsize(filepath)
        if final_size == 0:
            _cleanup_file(filepath)
            return False, "Merged file is empty", 0

        elapsed = time.time() - start_time
        avg_speed = final_size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        logger.info(
            "Multi-segment download: %.1f MB in %.1fs (%.1f MB/s)",
            final_size / 1024 / 1024, elapsed, avg_speed,
        )

        return True, "", final_size

    except Exception as e:
        logger.warning("Multi-segment download error: %s", e)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


async def _download_with_curl_cffi(
    url: str, filepath: str, referer: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """دانلود با curl_cffi."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        await progress_cb("📥 **شروع دانلود (curl_cffi)...**")

        async with AsyncSession() as session:
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

            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}", 0

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > MAX_DOWNLOAD_SIZE:
                return False, f"File too large: {content_length / 1024 / 1024:.0f} MB", 0

            downloaded = 0
            start_time = time.time()
            last_update = 0.0

            async with aiofiles.open(filepath, "wb") as f:
                async for chunk in resp.aiter_content():
                    if not chunk:
                        continue
                    await f.write(chunk)
                    downloaded += len(chunk)

                    if downloaded > MAX_DOWNLOAD_SIZE:
                        _cleanup_file(filepath)
                        return False, "Download exceeded size limit", 0

                    now = time.time()
                    if now - last_update >= PROGRESS_INTERVAL:
                        last_update = now
                        await progress_cb(
                            _format_progress(downloaded, content_length, start_time, now)
                        )

        if not os.path.exists(filepath):
            return False, "File not created", 0

        size = os.path.getsize(filepath)
        if size == 0:
            _cleanup_file(filepath)
            return False, "Downloaded file is empty", 0

        return True, "", size

    except Exception as e:
        logger.warning("curl_cffi download error: %s", e)
        return False, str(e)[:200], 0


async def _download_with_ytdlp(
    url: str, filepath: str, referer: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """دانلود با yt-dlp."""
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    has_aria2c = shutil.which("aria2c") is not None
    mode = "aria2c" if has_aria2c else "concurrent x16"
    await progress_cb(f"📥 **شروع دانلود (yt-dlp · {mode})...**")

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings", "--progress", "--newline",
            "--no-check-certificates",
            "-f", "best",
            "--concurrent-fragments", "16",
            "--retries", "10",
            "--fragment-retries", "10",
            "--buffer-size", "16K",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", f"Referer:{referer}",
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "-o", filepath,
        ]

        if has_aria2c:
            cmd.extend([
                "--downloader", "aria2c",
                "--downloader-args",
                "aria2c:-x16 -s16 -k1M --max-connection-per-server=16 "
                "--min-split-size=1M --console-log-level=warn",
            ])

        if _check_impersonation_support():
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
                    process.stdout.readline(), timeout=DOWNLOAD_READ_TIMEOUT
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
                await progress_cb(f"📥 **Downloading...**\n`{text[:80]}`")

        await process.wait()

        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            return False, stderr[:200], 0

        actual_path = _find_output_file(filepath)
        if not actual_path:
            return False, "Output file not found", 0

        size = os.path.getsize(actual_path)
        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(actual_path)
            return False, "File exceeds size limit", 0

        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        return False, str(e)[:200], 0


async def _download_with_aiohttp(
    url: str, filepath: str, referer: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """دانلود با aiohttp ساده."""
    headers = {
        **_DEFAULT_HEADERS,
        "Referer": referer,
    }

    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with _get_session(timeout) as session:
                async with session.get(
                    url, headers=headers, allow_redirects=True
                ) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if resp.status != 403 and 400 <= resp.status < 500:
                            _cleanup_file(filepath)
                            return False, error, 0
                    else:
                        content_length = int(resp.headers.get("Content-Length", 0))
                        if content_length > MAX_DOWNLOAD_SIZE:
                            return False, f"File too large: {content_length / 1024 / 1024:.0f} MB", 0

                        downloaded = 0
                        start_time = time.time()
                        last_update = 0.0

                        async with aiofiles.open(filepath, "wb") as f:
                            async for chunk in resp.content.iter_chunked(1024 * 1024):
                                await f.write(chunk)
                                downloaded += len(chunk)

                                if downloaded > MAX_DOWNLOAD_SIZE:
                                    _cleanup_file(filepath)
                                    return False, "Download exceeded size limit", 0

                                now = time.time()
                                if now - last_update >= PROGRESS_INTERVAL:
                                    last_update = now
                                    await progress_cb(
                                        _format_progress(downloaded, content_length, start_time, now)
                                    )

                        size = os.path.getsize(filepath)
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


# ─── Download: Public API ──────────────────────────────────


def _guess_referer(url: str) -> str:
    """حدس Referer مناسب."""
    try:
        host = urlparse(url).hostname or ""
        if "eporner" in host:
            return "https://www.eporner.com/"
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname}/"
    except Exception:
        return "https://www.eporner.com/"


async def download_eporner_direct(
    url: str, filepath: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """
    دانلود لینک مستقیم MP4.
    اول multi-segment، بعد curl_cffi، بعد yt-dlp، آخر aiohttp.
    """
    if not _is_video_cdn_url(url):
        return False, "URL host not allowed", 0

    referer = _guess_referer(url)

    # ── روش 1: multi-segment ──
    if _check_impersonation_support():
        logger.info("Download attempt 1: multi-segment")
        success, error, size = await _download_multi_segment(
            url, filepath, referer, progress_cb, num_segments=8
        )
        if success:
            return True, "", size
        logger.info("Multi-segment failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 2: curl_cffi ──
    if _check_impersonation_support():
        logger.info("Download attempt 2: curl_cffi single")
        success, error, size = await _download_with_curl_cffi(
            url, filepath, referer, progress_cb
        )
        if success:
            return True, "", size
        logger.info("curl_cffi failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 3: yt-dlp ──
    if shutil.which("yt-dlp"):
        logger.info("Download attempt 3: yt-dlp")
        success, error, size = await _download_with_ytdlp(
            url, filepath, referer, progress_cb
        )
        if success:
            return True, "", size
        logger.info("yt-dlp failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 4: aiohttp ──
    logger.info("Download attempt 4: aiohttp")
    success, error, size = await _download_with_aiohttp(
        url, filepath, referer, progress_cb
    )
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0


async def download_eporner_m3u8(
    url: str, filepath: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """دانلود M3U8 stream."""
    if not _is_video_cdn_url(url):
        return False, "URL host not allowed", 0

    if not shutil.which("yt-dlp"):
        return False, "yt-dlp is not installed", 0

    referer = _guess_referer(url)
    success, error, size = await _download_with_ytdlp(
        url, filepath, referer, progress_cb
    )
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0
