"""
xxxpublicpornvideos_handler.py
──────────────────────────────
استخراج و دانلود ویدیو از fa.xxxpublicpornvideos.com

روش کار (بر اساس تحلیل واقعی صفحه):
  - صفحه ویدیو HTML5 ساده با <video> و <source> داره
  - URL ویدیو از CDN خارجی (vs5.videosrc.net) میاد
  - URL امضا‌دار با md5 + expires (TTL کوتاه - حدود 12 ساعت)
  - سرور CDN از Range request پشتیبانی می‌کنه (HTTP 206)
  - کوکی‌های av و strg در session set می‌شن (احتیاطی استفاده می‌شن)
  - نیاز به Referer header از صفحه اصلی
  - yt-dlp با extractor=html5 هم کار می‌کنه ولی فقط 1 فرمت می‌ده

استراتژی دانلود:
  1. fetch صفحه + استخراج URL از <source> tag
  2. multi-segment download با curl_cffi/aiohttp (16 connection)
  3. fallback به yt-dlp
  4. fallback به aiohttp ساده

وابستگی‌ها:
    pip install aiohttp aiofiles curl_cffi yt-dlp
"""

import asyncio
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

logger = logging.getLogger("XxxPublicPornVideosHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
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
MIN_VALID_VIDEO_SIZE = 100 * 1024  # 100 KB (برای تشخیص صفحه خطا)
MAX_RETRIES = 3
RETRY_DELAY = 2.0
PROGRESS_INTERVAL = 2.0
CHUNK_SIZE = 1024 * 1024  # 1 MB

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset({
    "xxxpublicpornvideos.com",
    "www.xxxpublicpornvideos.com",
    "fa.xxxpublicpornvideos.com",
})

# دامنه‌های CDN مجاز برای دانلود
_ALLOWED_CDN_HOSTS = frozenset({
    "videosrc.net",
    "vs5.videosrc.net",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_xxxpublicpornvideos_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به xxxpublicpornvideos هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".xxxpublicpornvideos.com")
    except Exception:
        return False


def _is_allowed_cdn_url(url: str) -> bool:
    """بررسی اینکه URL به CDN مجاز اشاره می‌کنه."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_CDN_HOSTS or host.endswith(".videosrc.net")
    except Exception:
        return False


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
    """نظافت URL از HTML entities و کاراکترهای اضافی."""
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
    """استخراج عنوان ویدیو از HTML."""
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
        return m.group(1).strip()

    # روش 2: <title>
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        # حذف پسوندهای رایج
        title = re.sub(
            r"\s*[-|]\s*(?:XXXPublicPornVideos|xxxpublicpornvideos)\s*$",
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


def _extract_video_url(html: str) -> Optional[str]:
    """
    استخراج URL ویدیو از HTML.
    اولویت با <source> tag داخل <video> است.
    """
    # روش 1: <source src="..."> داخل <video>
    # این روش مطابق با ساختار واقعی سایت
    source_pattern = re.compile(
        r'<source\b[^>]*\ssrc=["\']([^"\']+)["\'][^>]*>',
        re.IGNORECASE,
    )
    matches = source_pattern.findall(html)
    for src in matches:
        clean = _clean_url(src)
        # فقط URL های mp4 از CDN رو قبول کن
        if ".mp4" in clean.lower() and (
            _is_allowed_cdn_url(clean) or "videosrc.net" in clean
        ):
            return clean

    # روش 2: هر URL mp4 که به CDN اشاره می‌کنه
    mp4_pattern = re.compile(
        r'(https?://[^\s"\'<>\)\]]+?\.mp4(?:\?[^\s"\'<>\)\]]*)?)',
        re.IGNORECASE,
    )
    for m in mp4_pattern.finditer(html):
        url = _clean_url(m.group(1))
        if "videosrc.net" in url or _is_allowed_cdn_url(url):
            # فیلتر preview/trailer
            if "preview" in url.lower() or "trailer" in url.lower():
                continue
            return url

    # روش 3: هر URL mp4 (بدون فیلتر CDN - آخرین راه)
    for m in mp4_pattern.finditer(html):
        url = _clean_url(m.group(1))
        if "preview" in url.lower() or "trailer" in url.lower():
            continue
        return url

    return None


async def _fetch_page(
    url: str,
    jar: Optional[CookieJar] = None,
) -> Tuple[Optional[str], Optional[CookieJar], str]:
    """
    دریافت HTML صفحه.

    اول curl_cffi (با impersonate) امتحان می‌شه، بعد aiohttp.

    Returns:
        (html, cookie_jar, error_message)
    """
    # ── روش 1: curl_cffi ──
    if _check_curl_cffi():
        try:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession() as session:
                resp = await session.get(
                    url,
                    impersonate="chrome",
                    headers={
                        "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
                    },
                    allow_redirects=True,
                    timeout=30,
                )
                if resp.status_code == 200 and resp.text:
                    logger.info("Page fetched via curl_cffi (impersonate=chrome)")
                    return resp.text, None, ""
                logger.warning(
                    "curl_cffi fetch: HTTP %s", resp.status_code
                )
        except Exception as e:
            logger.warning("curl_cffi fetch error: %s", e)

    # ── روش 2: aiohttp ──
    try:
        local_jar = jar or CookieJar(unsafe=True)
        timeout = ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=_DEFAULT_HEADERS,
            cookie_jar=local_jar,
        ) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="replace")
                    logger.info("Page fetched via aiohttp")
                    return html, local_jar, ""
                return None, local_jar, f"HTTP {resp.status}"
    except Exception as e:
        return None, jar, str(e)[:200]


async def extract_xxxpublicpornvideos_qualities(
    url: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict]:
    """
    استخراج کیفیت‌های ویدیو.

    توجه: این سایت فقط یک کیفیت MP4 ارائه می‌ده.

    Returns:
        (qualities, title, info)
        qualities: list of dicts with keys: label, url, height, quality_key, method
        title: str
        info: dict with extra info (thumbnail, etc.)
    """
    if not is_xxxpublicpornvideos_url(url):
        return [], "Invalid URL", {}

    if progress_cb:
        await progress_cb("🔄 **دریافت اطلاعات صفحه...**")

    # fetch صفحه با cookie jar جدا برای حفظ session
    jar = CookieJar(unsafe=True)
    html, jar, error = await _fetch_page(url, jar)

    if not html:
        logger.error("Failed to fetch page: %s", error)
        return [], f"خطا در دریافت صفحه: {error}", {}

    # استخراج اطلاعات
    title = _extract_title(html)
    thumbnail = _extract_thumbnail(html)
    video_url = _extract_video_url(html)

    if not video_url:
        logger.error("Video URL not found in page")
        return [], "URL ویدیو در صفحه پیدا نشد", {}

    logger.info("Found video URL: %s", video_url[:100])

    qualities = [
        {
            "label": "📺 MP4",
            "url": video_url,
            "height": 720,  # تخمینی - سایت فقط یک کیفیت داره
            "quality_key": "default",
            "method": "direct",
        }
    ]

    if progress_cb:
        await progress_cb(f"✅ **پیدا شد:** {title[:50]}")

    return qualities, title, {
        "thumbnail": thumbnail,
        "page_url": url,
    }


# ─── Download: Multi-segment (fast) ────────────────────────────────────────


async def _download_multi_segment(
    direct_url: str,
    filepath: str,
    referer: str,
    progress_cb: ProgressCallback,
    num_segments: int = 16,
) -> Tuple[bool, str, int]:
    """
    دانلود چند تیکه‌ای با چند connection همزمان.
    از aiohttp استفاده می‌کنه چون CDN ساده‌ست و impersonate لازم نیست.
    """
    headers = {
        **_DEFAULT_HEADERS,
        "Referer": referer,
        "Accept": "*/*",
    }

    try:
        # ── HEAD request برای فهمیدن حجم و Range support ──
        timeout = ClientTimeout(total=30, connect=15)
        async with aiohttp.ClientSession(
            timeout=timeout, headers=headers
        ) as session:
            async with session.head(direct_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HEAD failed: HTTP {resp.status}", 0

                content_length = int(resp.headers.get("Content-Length", 0))
                accept_ranges = resp.headers.get("Accept-Ranges", "")

                if content_length == 0:
                    return False, "Cannot determine file size", 0
                if content_length > MAX_DOWNLOAD_SIZE:
                    return (
                        False,
                        f"File too large: {_format_size(content_length)}",
                        0,
                    )
                if accept_ranges.lower() != "bytes":
                    logger.info("Server doesn't support Range requests")
                    return False, "Range not supported", 0

        total_mb = content_length / 1024 / 1024
        await progress_cb(
            f"📥 **دانلود سریع ({num_segments} بخش)...**\n"
            f"💾 حجم: {total_mb:.1f} MB"
        )

        # ── ساخت segment ها ──
        segment_size = content_length // num_segments
        segments = []
        for i in range(num_segments):
            start = i * segment_size
            end = (
                content_length - 1
                if i == num_segments - 1
                else (i + 1) * segment_size - 1
            )
            segments.append((i, start, end))

        segment_files = [f"{filepath}.part{i}" for i in range(num_segments)]
        downloaded_bytes = [0] * num_segments
        start_time = time.time()
        last_update = [0.0]
        lock = asyncio.Lock()

        async def _download_segment(seg_idx: int, byte_start: int, byte_end: int):
            seg_file = segment_files[seg_idx]
            seg_timeout = ClientTimeout(total=3600, connect=30, sock_read=120)

            for attempt in range(MAX_RETRIES):
                try:
                    async with aiohttp.ClientSession(
                        timeout=seg_timeout, headers=headers
                    ) as session:
                        req_headers = {"Range": f"bytes={byte_start}-{byte_end}"}
                        async with session.get(
                            direct_url, headers=req_headers, allow_redirects=True
                        ) as resp:
                            if resp.status not in (200, 206):
                                raise Exception(f"HTTP {resp.status}")

                            async with aiofiles.open(seg_file, "wb") as f:
                                async for chunk in resp.content.iter_chunked(
                                    CHUNK_SIZE
                                ):
                                    if not chunk:
                                        continue
                                    await f.write(chunk)
                                    downloaded_bytes[seg_idx] += len(chunk)

                                    now = time.time()
                                    async with lock:
                                        if (
                                            now - last_update[0] >= PROGRESS_INTERVAL
                                            and progress_cb
                                        ):
                                            last_update[0] = now
                                            total_dl = sum(downloaded_bytes)
                                            await progress_cb(
                                                _format_progress(
                                                    total_dl,
                                                    content_length,
                                                    start_time,
                                                    now,
                                                )
                                            )
                    return
                except asyncio.CancelledError:
                    _cleanup_file(seg_file)
                    raise
                except Exception as e:
                    logger.warning(
                        "Segment %d attempt %d failed: %s",
                        seg_idx, attempt + 1, e,
                    )
                    _cleanup_file(seg_file)
                    downloaded_bytes[seg_idx] = 0
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))

            raise Exception(
                f"Segment {seg_idx} failed after {MAX_RETRIES} attempts"
            )

        # ── دانلود همه segment ها به صورت موازی ──
        try:
            await asyncio.gather(
                *[
                    _download_segment(idx, start, end)
                    for idx, start, end in segments
                ]
            )
        except Exception as e:
            for sf in segment_files:
                _cleanup_file(sf)
            return False, str(e)[:200], 0

        # ── ترکیب segment ها ──
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

        # ── بررسی نهایی ──
        final_size = os.path.getsize(filepath)
        if final_size == 0:
            _cleanup_file(filepath)
            return False, "Merged file is empty", 0

        if final_size < MIN_VALID_VIDEO_SIZE:
            _cleanup_file(filepath)
            return False, f"File too small ({final_size} bytes)", 0

        elapsed = time.time() - start_time
        avg_speed = final_size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        logger.info(
            "Multi-segment download complete: %.1f MB in %.1fs (%.1f MB/s)",
            final_size / 1024 / 1024,
            elapsed,
            avg_speed,
        )

        return True, "", final_size

    except Exception as e:
        logger.warning("Multi-segment download error: %s", e)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Download: curl_cffi (single connection) ───────────────────────────────


async def _download_with_curl_cffi(
    url: str,
    filepath: str,
    referer: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود با curl_cffi (single connection)."""
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
                return (
                    False,
                    f"File too large: {content_length / 1024 / 1024:.0f} MB",
                    0,
                )

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
                            _format_progress(
                                downloaded, content_length, start_time, now
                            )
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


# ─── Download: aiohttp (simple) ────────────────────────────────────────────


async def _download_with_aiohttp(
    url: str,
    filepath: str,
    referer: str,
    progress_cb: ProgressCallback,
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
            async with aiohttp.ClientSession(
                timeout=timeout, headers=headers
            ) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if 400 <= resp.status < 500:
                            _cleanup_file(filepath)
                            return False, error, 0
                    else:
                        content_length = int(
                            resp.headers.get("Content-Length", 0)
                        )
                        if content_length > MAX_DOWNLOAD_SIZE:
                            return (
                                False,
                                f"File too large: "
                                f"{content_length / 1024 / 1024:.0f} MB",
                                0,
                            )

                        downloaded = 0
                        start_time = time.time()
                        last_update = 0.0

                        async with aiofiles.open(filepath, "wb") as f:
                            async for chunk in resp.content.iter_chunked(
                                CHUNK_SIZE
                            ):
                                await f.write(chunk)
                                downloaded += len(chunk)

                                if downloaded > MAX_DOWNLOAD_SIZE:
                                    _cleanup_file(filepath)
                                    return (
                                        False,
                                        "Download exceeded size limit",
                                        0,
                                    )

                                now = time.time()
                                if now - last_update >= PROGRESS_INTERVAL:
                                    last_update = now
                                    await progress_cb(
                                        _format_progress(
                                            downloaded,
                                            content_length,
                                            start_time,
                                            now,
                                        )
                                    )

                        size = os.path.getsize(filepath)
                        if size < MIN_VALID_VIDEO_SIZE:
                            _cleanup_file(filepath)
                            return (
                                False,
                                f"File too small ({size} bytes)",
                                0,
                            )

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


# ─── Download: yt-dlp (fallback) ──────────────────────────────────────────


async def _download_with_ytdlp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود با yt-dlp (fallback)."""
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    has_aria2c = shutil.which("aria2c") is not None
    mode = "aria2c" if has_aria2c else "concurrent x16"
    await progress_cb(f"📥 **شروع دانلود (yt-dlp · {mode})...**")

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--progress",
            "--newline",
            "--no-check-certificates",
            "-f", "best",
            "--concurrent-fragments", "16",
            "--retries", "10",
            "--fragment-retries", "10",
            "--buffer-size", "16K",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
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

        if _check_curl_cffi():
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
                    process.stdout.readline(), timeout=120
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
            return False, stderr[:200], 0

        # پیدا کردن فایل خروجی
        actual_path = _find_output_file(filepath)
        if not actual_path:
            return False, "Output file not found", 0

        size = os.path.getsize(actual_path)
        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(actual_path)
            return False, "File exceeds size limit", 0

        if actual_path != filepath:
            try:
                os.rename(actual_path, filepath)
            except OSError:
                pass

        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        return False, str(e)[:200], 0


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


# ─── Public API ────────────────────────────────────────────────────────────


async def download_xxxpublicpornvideos_video(
    page_url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو از xxxpublicpornvideos.

    استراتژی:
      1. multi-segment download (16 connection, سریع‌ترین)
      2. curl_cffi (single connection, برای زمانی که CDN مشکل داره)
      3. yt-dlp (fallback پایدار)
      4. aiohttp (آخرین راه)

    Args:
        page_url: URL صفحه ویدیو (برای Referer)
        video_url: URL مستقیم ویدیو (از extract گرفته شده)
        filepath: مسیر ذخیره فایل
        progress_cb: callback برای گزارش پیشرفت

    Returns:
        (success, error_message, file_size)
    """
    if not is_xxxpublicpornvideos_url(page_url):
        return False, "URL host not allowed", 0

    if not video_url:
        return False, "Empty video URL", 0

    if progress_cb is None:
        async def _noop(msg: str) -> None:
            pass
        progress_cb = _noop

    referer = page_url

    # ── روش 1: multi-segment (سریع‌ترین) ──
    logger.info("Download attempt 1: multi-segment")
    success, error, size = await _download_multi_segment(
        video_url, filepath, referer, progress_cb, num_segments=16
    )
    if success:
        return True, "", size
    logger.info("Multi-segment failed: %s", error)
    _cleanup_file(filepath)

    # ── روش 2: curl_cffi ──
    if _check_curl_cffi():
        logger.info("Download attempt 2: curl_cffi single")
        success, error, size = await _download_with_curl_cffi(
            video_url, filepath, referer, progress_cb
        )
        if success:
            return True, "", size
        logger.info("curl_cffi failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 3: yt-dlp ──
    if shutil.which("yt-dlp"):
        logger.info("Download attempt 3: yt-dlp")
        success, error, size = await _download_with_ytdlp(
            video_url, filepath, progress_cb
        )
        if success:
            return True, "", size
        logger.info("yt-dlp failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 4: aiohttp ──
    logger.info("Download attempt 4: aiohttp")
    success, error, size = await _download_with_aiohttp(
        video_url, filepath, referer, progress_cb
    )
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0


async def download_xxxpublicpornvideos_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    """Wrapper برای سازگاری با bot architecture."""
    if not video_url:
        # اگه video_url داده نشده، از صفحه استخراج کن
        qualities, title, info = await extract_xxxpublicpornvideos_qualities(
            url, progress_cb
        )
        if not qualities:
            return False, title or "Extraction failed", 0
        video_url = qualities[0]["url"]

    return await download_xxxpublicpornvideos_video(
        url, video_url, filepath, progress_cb
    )
