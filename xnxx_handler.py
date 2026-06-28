"""
xnxx_handler.py
---------------
استخراج لینک‌های دانلود از xnxx.com و ارسال ویدیو به کاربر.

روش کار:
  - لینک‌های مستقیم MP4 (360p, 240p) از HTML صفحه استخراج میشن
  - M3U8 stream ها (1080p, 720p, 480p) با yt-dlp دانلود میشن
  - کاربر با دکمه کیفیت انتخاب میکنه
"""

import asyncio
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("XNXXHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# حداکثر حجم دانلود: 2 گیگابایت
MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

# حداکثر عمر session (ثانیه): 30 دقیقه
SESSION_TTL = 30 * 60

# حداکثر تعداد retry
MAX_RETRIES = 3
RETRY_DELAY = 2.0

# دامنه‌های مجاز
_ALLOWED_HOSTS = {"xnxx.com", "www.xnxx.com", "cdn77-vid.xnxx-cdn.com"}

# session های در حال انتظار: key = session_id
xnxx_sessions: Dict[str, dict] = {}

# تایپ callback پیشرفت
ProgressCallback = Callable[[str], Awaitable[None]]


def _is_allowed_host(url: str) -> bool:
    """بررسی اینکه URL به دامنه‌های مجاز اشاره میکنه."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # اجازه به ساب‌دامنه‌های xnxx
        return (
            host in _ALLOWED_HOSTS
            or host.endswith(".xnxx.com")
            or host.endswith(".xnxx-cdn.com")
        )
    except Exception:
        return False


def is_xnxx_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به xnxx هست."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return host in ("xnxx.com", "www.xnxx.com") or host.endswith(".xnxx.com")
    except Exception:
        return False


def cleanup_expired_sessions() -> int:
    """پاکسازی session های منقضی شده. تعداد پاک‌شده رو برمیگردونه."""
    now = time.time()
    expired = [
        sid for sid, data in xnxx_sessions.items()
        if now - data.get("created_at", 0) > SESSION_TTL
    ]
    for sid in expired:
        xnxx_sessions.pop(sid, None)
    if expired:
        logger.info("Cleaned up %d expired sessions", len(expired))
    return len(expired)


@asynccontextmanager
async def _get_session(timeout: Optional[ClientTimeout] = None):
    """ساخت و مدیریت aiohttp session با cleanup خودکار."""
    t = timeout or ClientTimeout(total=30, connect=10)
    session = aiohttp.ClientSession(timeout=t, headers=_DEFAULT_HEADERS)
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
    """
    دریافت محتوای URL با retry خودکار.

    Returns:
        (content, status_code) - اگه موفق نبود content=None
    """
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            async with _get_session(timeout) as session:
                merged_headers = {**_DEFAULT_HEADERS, **(headers or {})}
                async with session.get(
                    url, headers=merged_headers, allow_redirects=True
                ) as resp:
                    if resp.status == 200:
                        content = await resp.text(errors="replace")
                        return content, 200
                    last_error = f"HTTP {resp.status}"
                    # برای 4xx retry نکن
                    if 400 <= resp.status < 500:
                        logger.warning("Client error %d for %s", resp.status, url)
                        return None, resp.status
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = str(e)[:120]
            logger.warning(
                "Attempt %d/%d failed for %s: %s",
                attempt, max_retries, url, last_error,
            )

        if attempt < max_retries:
            await asyncio.sleep(RETRY_DELAY * attempt)

    logger.error("All %d attempts failed for %s: %s", max_retries, url, last_error)
    return None, 0


def _cleanup_file(filepath: str) -> None:
    """حذف فایل اگه وجود داشته باشه."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Cleaned up file: %s", filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


async def extract_xnxx_qualities(url: str) -> Tuple[List[dict], str]:
    """
    لینک‌های کیفیت مختلف رو از صفحه xnxx استخراج میکنه.

    Returns:
        (qualities, title)
        qualities: لیست dict با کلیدهای: label, url, method ('direct' یا 'm3u8')
        title: عنوان ویدیو
    """
    if not is_xnxx_url(url):
        logger.warning("URL is not a valid xnxx URL: %s", url)
        return [], "Invalid URL"

    # پاکسازی session های قدیمی
    cleanup_expired_sessions()

    html, status = await _fetch_with_retry(url)
    if html is None:
        return [], f"HTTP {status}" if status else "Connection failed"

    # عنوان ویدیو
    title = _extract_title(html)
    qualities: List[dict] = []

    # ── لینک‌های مستقیم MP4 ──────────────────────────────────
    _extract_direct_mp4(html, qualities)

    # ── M3U8 stream ها ───────────────────────────────────────
    await _extract_m3u8_streams(html, qualities)

    # مرتب‌سازی: کیفیت بالاتر اول
    qualities.sort(key=_quality_sort_key, reverse=True)

    logger.info(
        "Extracted %d qualities for: %s", len(qualities), title[:60]
    )
    return qualities, title


def _extract_title(html: str) -> str:
    """استخراج عنوان ویدیو از HTML."""
    title_m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if not title_m:
        return "Untitled"
    title = title_m.group(1).strip()
    title = re.sub(
        r"\s*[-|]\s*XNXX\.COM.*$", "", title, flags=re.IGNORECASE
    ).strip()
    return title or "Untitled"


def _extract_direct_mp4(html: str, qualities: List[dict]) -> None:
    """استخراج لینک‌های مستقیم MP4 از HTML."""
    patterns = [
        (r"setVideoUrl\s*\(\s*(\d+)\s*,\s*'([^']+)'", True),
        (r"setVideoUrlHigh\s*\(\s*'([^']+)'", False),
        (r"setVideoUrlLow\s*\(\s*'([^']+)'", False),
        (
            r'html5video\.(?:setVideoUrl|mp4)\s*[=(]\s*["\']'
            r'([^"\']+\.mp4[^"\']*)["\']',
            False,
        ),
    ]

    for pattern, has_quality_num in patterns:
        for m in re.finditer(pattern, html):
            if has_quality_num and m.lastindex == 2:
                quality_num = m.group(1)
                video_url = m.group(2)
                label = f"🎥 MP4 {quality_num}p"
            else:
                video_url = m.group(1)
                label = "🎥 MP4"

            video_url = _normalize_url(video_url)
            if not video_url:
                continue

            # بررسی امنیتی
            if not _is_allowed_host(video_url):
                logger.warning("Blocked URL with disallowed host: %s", video_url)
                continue

            # جلوگیری از تکراری
            if any(q["url"] == video_url for q in qualities):
                continue

            qualities.append({
                "label": label,
                "url": video_url,
                "method": "direct",
            })


async def _extract_m3u8_streams(html: str, qualities: List[dict]) -> None:
    """استخراج M3U8 stream ها از HTML."""
    m3u8_patterns = [
        r"setVideoHLS\s*\(\s*'([^']+\.m3u8[^']*)'",
        r'"hls"\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r"'hls'\s*:\s*'([^']+\.m3u8[^']*)'",
        r'url\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    ]

    for pattern in m3u8_patterns:
        for m in re.finditer(pattern, html):
            m3u8_url = _normalize_url(m.group(1))
            if not m3u8_url:
                continue

            if not _is_allowed_host(m3u8_url):
                logger.warning("Blocked M3U8 URL with disallowed host: %s", m3u8_url)
                continue

            if any(q["url"] == m3u8_url for q in qualities):
                continue

            # سعی کنیم کیفیت‌های مختلف M3U8 رو بخونیم
            sub_qualities = await _parse_m3u8_variants(m3u8_url)
            if sub_qualities:
                for sq in sub_qualities:
                    if not any(q["url"] == sq["url"] for q in qualities):
                        qualities.append(sq)
            else:
                qualities.append({
                    "label": "📡 M3U8 Stream",
                    "url": m3u8_url,
                    "method": "m3u8",
                })


def _normalize_url(url: str) -> Optional[str]:
    """نرمال‌سازی URL. اگه نامعتبر بود None برمیگردونه."""
    url = url.replace("\\/", "/").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return None
    return url


def _quality_sort_key(q: dict) -> int:
    """کلید مرتب‌سازی بر اساس عدد کیفیت."""
    nums = re.findall(r"\d+", q["label"])
    return int(nums[-1]) if nums else 0


async def _parse_m3u8_variants(master_url: str) -> List[dict]:
    """
    M3U8 master playlist رو پارس میکنه و کیفیت‌های مختلف رو برمیگردونه.
    """
    timeout = ClientTimeout(total=15, connect=8)
    content, status = await _fetch_with_retry(
        master_url, max_retries=2, timeout=timeout
    )
    if content is None:
        return []

    # اگه master playlist نیست (stream مستقیم)
    if "#EXT-X-STREAM-INF" not in content:
        return []

    base_url = master_url.rsplit("/", 1)[0] + "/"
    results = []
    lines = content.splitlines()

    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        if i + 1 >= len(lines):
            continue

        stream_uri = lines[i + 1].strip()
        if not stream_uri or stream_uri.startswith("#"):
            continue

        if not stream_uri.startswith("http"):
            stream_uri = base_url + stream_uri

        if not _is_allowed_host(stream_uri):
            logger.warning("Blocked M3U8 variant with disallowed host: %s", stream_uri)
            continue

        # استخراج RESOLUTION و BANDWIDTH
        res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bw_m = re.search(r"BANDWIDTH=(\d+)", line)

        if res_m:
            height = int(res_m.group(2))
            label = f"📡 M3U8 {height}p"
        elif bw_m:
            bw_kb = int(bw_m.group(1)) // 1000
            label = f"📡 M3U8 ~{bw_kb}kbps"
        else:
            label = "📡 M3U8 Stream"

        results.append({
            "label": label,
            "url": stream_uri,
            "method": "m3u8",
        })

    return results


async def download_xnxx_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """
    دانلود لینک مستقیم MP4.

    Returns:
        (success, error_message, file_size)
    """
    if not _is_allowed_host(url):
        return False, "URL host not allowed", 0

    headers = {
        **_DEFAULT_HEADERS,
        "Referer": "https://www.xnxx.com/",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            success, error, size = await _do_direct_download(
                url, filepath, headers, progress_cb
            )
            if success:
                return True, "", size

            # برای خطاهای HTTP 4xx retry نکن
            if error.startswith("HTTP 4"):
                _cleanup_file(filepath)
                return False, error, 0

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            error = str(e)[:150]
            logger.warning(
                "Download attempt %d/%d failed: %s", attempt, MAX_RETRIES, error
            )

        if attempt < MAX_RETRIES:
            _cleanup_file(filepath)
            await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)
    return False, f"Failed after {MAX_RETRIES} attempts: {error}", 0


async def _do_direct_download(
    url: str,
    filepath: str,
    headers: dict,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """اجرای واقعی دانلود مستقیم."""
    timeout = ClientTimeout(total=3600, connect=30, sock_read=120)

    async with _get_session(timeout) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}", 0

            content_length = int(resp.headers.get("Content-Length", 0))

            # بررسی محدودیت حجم
            if content_length > MAX_DOWNLOAD_SIZE:
                size_mb = content_length / 1024 / 1024
                return False, f"File too large: {size_mb:.0f} MB", 0

            downloaded = 0
            start_time = time.time()
            last_update = 0.0

            async with aiofiles.open(filepath, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    await f.write(chunk)
                    downloaded += len(chunk)

                    # بررسی محدودیت حجم حین دانلود
                    if downloaded > MAX_DOWNLOAD_SIZE:
                        _cleanup_file(filepath)
                        return False, "Download exceeded size limit", 0

                    now = time.time()
                    if now - last_update >= 2.0:
                        last_update = now
                        text = _format_progress(
                            downloaded, content_length, start_time, now
                        )
                        await progress_cb(text)

    size = os.path.getsize(filepath)
    return True, "", size


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
        return (
            f"📥 **Downloading...**\n`[{bar}]`\n"
            f"💾 {dl_mb:.1f}/{total_mb:.1f} MB"
            f"  •  ⚡ {speed / 1024 / 1024:.1f} MB/s\n📊 {pct:.1f}%"
        )
    return (
        f"📥 **Downloading...**\n"
        f"💾 {dl_mb:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
    )


async def download_xnxx_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """
    دانلود M3U8 stream با yt-dlp.

    Returns:
        (success, error_message, file_size)
    """
    if not _is_allowed_host(m3u8_url):
        return False, "URL host not allowed", 0

    # بررسی نصب بودن yt-dlp
    if not shutil.which("yt-dlp"):
        logger.error("yt-dlp is not installed or not in PATH")
        return False, "yt-dlp is not installed", 0

    await progress_cb("📡 **دانلود M3U8 stream...**")

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--quiet",
            "--progress",
            "--newline",
            "-f", "best",
            "--hls-prefer-native",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", f"Referer:https://www.xnxx.com/",
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "-o", filepath,
            m3u8_url,
        ]

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
                logger.warning("yt-dlp stdout read timed out, killing process")
                process.kill()
                await process.wait()
                _cleanup_file(filepath)
                return False, "Download timed out", 0

            if not line:
                break

            text = line.decode(errors="replace").strip()
            now = time.time()
            if now - last_update >= 2.0 and text:
                last_update = now
                await progress_cb(f"📡 **Downloading M3U8...**\n`{text[:80]}`")

        await process.wait()

        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            logger.error("yt-dlp failed (code %d): %s", process.returncode, stderr[:200])
            _cleanup_file(filepath)
            return False, stderr[:200], 0

        # yt-dlp ممکنه پسوند متفاوت بذاره
        filepath = _find_output_file(filepath)
        if not filepath:
            return False, "Output file not found", 0

        size = os.path.getsize(filepath)

        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(filepath)
            return False, "Downloaded file exceeds size limit", 0

        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.exception("M3U8 download error")
        _cleanup_file(filepath)
        return False, str(e)[:150], 0


def _find_output_file(filepath: str) -> Optional[str]:
    """پیدا کردن فایل خروجی yt-dlp (ممکنه پسوند متفاوت باشه)."""
    if os.path.exists(filepath):
        return filepath

    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = base + ext
        if os.path.exists(candidate):
            # تغییر نام به filepath اصلی
            try:
                os.rename(candidate, filepath)
                return filepath
            except OSError:
                return candidate

    return None
