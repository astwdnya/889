"""
cartoonporn_handler.py
──────────────────────
استخراج و دانلود ویدیو از cartoonporn.pro (KVS Player)

ویژگی‌ها:
  - استخراج لینک MP4 مستقیم از HTML (بدون yt-dlp)
  - fallback به yt-dlp اگه استخراج مستقیم fail شد
  - سرعت محدود سمت سرور (~100-120 KB/s)
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("CartoonPornHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
SESSION_TTL = 30 * 60
MAX_RETRIES = 3
RETRY_DELAY = 2.0
YTDLP_INFO_TIMEOUT = 90

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset(
    {
        "cartoonporn.pro",
        "www.cartoonporn.pro",
    }
)

cartoonporn_sessions: Dict[str, dict] = {}
ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_cartoonporn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به cartoonporn.pro هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS
    except Exception:
        return False


def cleanup_expired_sessions() -> int:
    """پاکسازی session های منقضی."""
    now = time.time()
    expired = [
        sid
        for sid, data in cartoonporn_sessions.items()
        if now - data.get("created_at", 0) > SESSION_TTL
    ]
    for sid in expired:
        cartoonporn_sessions.pop(sid, None)
    if expired:
        logger.info("Cleaned up %d expired sessions", len(expired))
    return len(expired)


def _cleanup_file(filepath: str) -> None:
    """حذف فایل."""
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


def _check_ytdlp() -> bool:
    return shutil.which("yt-dlp") is not None


# ─── HTTP helpers ───────────────────────────────────────────


async def _fetch_page(url: str) -> Tuple[Optional[str], int]:
    """دریافت HTML صفحه با retry."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=30, connect=10)
            async with aiohttp.ClientSession(
                timeout=timeout, headers=_DEFAULT_HEADERS
            ) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        return await resp.text(errors="replace"), 200
                    if 400 <= resp.status < 500:
                        return None, resp.status
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Fetch attempt %d/%d failed: %s", attempt, MAX_RETRIES, e
            )
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None, 0


# ─── Extraction: HTML direct ───────────────────────────────


def _extract_from_html(html: str, page_url: str) -> Tuple[List[dict], str]:
    """
    استخراج لینک ویدیو و عنوان از HTML صفحه KVS Player.

    Returns:
        (qualities, title)
    """
    # عنوان
    title = "Untitled"
    title_m = re.search(
        r'property=["\']og:title["\']\s+content=["\']([^"\']+)',
        html, re.IGNORECASE,
    )
    if not title_m:
        title_m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if title_m:
        title = title_m.group(1).strip()
        title = re.sub(r"\s*[-|].*$", "", title).strip() or title

    qualities: List[dict] = []
    seen_urls = set()

    # الگوی 1: flashvars video_url
    # video_url: 'https://...mp4...'
    fv_patterns = [
        r"video_url\s*:\s*'([^']+\.mp4[^']*)'",
        r"video_url\s*:\s*\"([^\"]+\.mp4[^\"]*)\"",
        r'video_url\s*=\s*"([^"]+\.mp4[^"]*)"',
        r"video_url\s*=\s*'([^']+\.mp4[^']*)'",
    ]
    for pattern in fv_patterns:
        for m in re.finditer(pattern, html):
            url = m.group(1).strip()
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # تشخیص کیفیت از URL
            height = _detect_height(url)
            label = f"📺 {height}p (MP4)" if height else "📺 MP4"

            qualities.append({
                "label": label,
                "url": url,
                "method": "direct",
                "height": height or 720,
            })

    # الگوی 2: لینک‌های get_file مستقیم MP4
    mp4_pattern = r'(https?://cartoonporn\.pro/get_file/[^\s"\'<>]+\.mp4[^\s"\'<>]*)'
    for m in re.finditer(mp4_pattern, html):
        url = m.group(1).strip()
        # فیلتر preview ها
        if "_preview" in url or "preview_" in url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        height = _detect_height(url)
        label = f"📺 {height}p (MP4)" if height else "📺 MP4"

        qualities.append({
            "label": label,
            "url": url,
            "method": "direct",
            "height": height or 720,
        })

    # الگوی 3: function/0/ wrapper (KVS specific)
    func_pattern = r'function/0/(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)'
    for m in re.finditer(func_pattern, html):
        url = m.group(1).strip()
        if "_preview" in url or url in seen_urls:
            continue
        seen_urls.add(url)

        height = _detect_height(url)
        label = f"📺 {height}p (MP4)" if height else "📺 MP4"

        qualities.append({
            "label": label,
            "url": url,
            "method": "direct",
            "height": height or 720,
        })

    # حذف تکراری بر اساس height
    unique = {}
    for q in qualities:
        h = q.get("height", 0)
        if h not in unique:
            unique[h] = q
    qualities = sorted(unique.values(), key=lambda q: q.get("height", 0), reverse=True)

    return qualities, title


def _detect_height(url: str) -> Optional[int]:
    """تشخیص کیفیت از URL."""
    m = re.search(r"_(\d{3,4})p", url)
    if m:
        return int(m.group(1))
    m = re.search(r"/(\d{3,4})p", url)
    if m:
        return int(m.group(1))
    return None


# ─── Extraction: yt-dlp fallback ───────────────────────────


async def _extract_with_ytdlp(url: str) -> Tuple[List[dict], str]:
    """Fallback: استخراج با yt-dlp."""
    if not _check_ytdlp():
        return [], "yt-dlp not installed"

    cmd = [
        "yt-dlp", "--dump-json", "--no-warnings",
        "--no-download", "--no-playlist",
        "--user-agent", _USER_AGENT, url,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=YTDLP_INFO_TIMEOUT,
        )

        if process.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            return [], err[:200]

        data = json.loads(stdout.decode(errors="replace"))
        title = data.get("title", "Untitled")
        formats = data.get("formats", [])

        qualities = []
        for fmt in formats:
            format_id = fmt.get("format_id", "")
            resolution = fmt.get("resolution", "")
            height = fmt.get("height")
            direct_url = fmt.get("url", "")

            if not height:
                h_m = re.search(r"(\d+)p", resolution)
                if h_m:
                    height = int(h_m.group(1))

            label = f"📺 {height}p (MP4)" if height else f"📺 {format_id}"

            qualities.append({
                "label": label,
                "url": direct_url,
                "format_id": format_id,
                "method": "ytdlp",
                "height": height or 0,
            })

        qualities.sort(key=lambda q: q.get("height", 0), reverse=True)
        return qualities, title

    except Exception as e:
        return [], str(e)[:200]


# ─── Main extraction ───────────────────────────────────────


async def extract_cartoonporn_qualities(url: str) -> Tuple[List[dict], str]:
    """
    استخراج کیفیت‌های موجود.

    اول HTML مستقیم، بعد fallback به yt-dlp.
    """
    if not is_cartoonporn_url(url):
        return [], "Invalid URL"

    cleanup_expired_sessions()

    # روش 1: استخراج مستقیم از HTML
    html, status = await _fetch_page(url)
    if html:
        qualities, title = _extract_from_html(html, url)
        if qualities:
            logger.info(
                "Extracted %d qualities from HTML for: %s",
                len(qualities), title[:60],
            )
            return qualities, title
        logger.info("No qualities from HTML, trying yt-dlp")

    # روش 2: yt-dlp
    qualities, title = await _extract_with_ytdlp(url)
    if qualities:
        logger.info(
            "Extracted %d qualities from yt-dlp for: %s",
            len(qualities), title[:60],
        )
    return qualities, title


# ─── Download ───────────────────────────────────────────────


async def download_cartoonporn_video(
    url: str,
    video_url: str,
    filepath: str,
    method: str = "direct",
    format_id: str = "",
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو.

    Args:
        url: آدرس صفحه اصلی
        video_url: لینک مستقیم ویدیو (یا URL صفحه برای ytdlp)
        filepath: مسیر فایل خروجی
        method: "direct" یا "ytdlp"
        format_id: شناسه فرمت (برای ytdlp)
        progress_cb: callback پیشرفت
    """
    if not is_cartoonporn_url(url):
        return False, "URL not allowed", 0

    if method == "direct" and video_url:
        success, error, size = await _download_direct(
            video_url, url, filepath, progress_cb,
        )
        if success:
            return True, "", size
        logger.info("Direct download failed: %s, trying yt-dlp", error)

    # Fallback: yt-dlp
    return await _download_with_ytdlp(url, format_id, filepath, progress_cb)


async def _download_direct(
    video_url: str,
    referer: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback],
) -> Tuple[bool, str, int]:
    """دانلود مستقیم با aiohttp."""
    headers = {
        **_DEFAULT_HEADERS,
        "Referer": referer,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with aiohttp.ClientSession(
                timeout=timeout, headers=headers
            ) as session:
                async with session.get(
                    video_url, allow_redirects=True
                ) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if 400 <= resp.status < 500:
                            return False, error, 0
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(RETRY_DELAY * attempt)
                            continue
                        return False, error, 0

                    content_length = int(
                        resp.headers.get("Content-Length", 0)
                    )
                    if content_length > MAX_DOWNLOAD_SIZE:
                        return False, f"File too large: {_format_size(content_length)}", 0

                    total_mb = content_length / 1024 / 1024 if content_length else 0
                    downloaded = 0
                    start_time = time.time()
                    last_update = 0.0

                    async with aiofiles.open(filepath, "wb") as f:
                        async for chunk in resp.content.iter_chunked(256 * 1024):
                            await f.write(chunk)
                            downloaded += len(chunk)

                            if downloaded > MAX_DOWNLOAD_SIZE:
                                _cleanup_file(filepath)
                                return False, "Download exceeded size limit", 0

                            now = time.time()
                            if progress_cb and now - last_update >= 2.0:
                                last_update = now
                                elapsed = now - start_time
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                dl_mb = downloaded / 1024 / 1024
                                speed_kb = min(speed / 1024, 99999)

                                if content_length > 0:
                                    pct = downloaded / content_length * 100
                                    filled = int(pct / 5)
                                    bar = "█" * filled + "░" * (20 - filled)
                                    eta_secs = int(
                                        (content_length - downloaded) / speed
                                    ) if speed > 0 else 0
                                    eta_m, eta_s = divmod(eta_secs, 60)

                                    await progress_cb(
                                        f"📥 **Downloading...**\n"
                                        f"`[{bar}]`\n"
                                        f"💾 {dl_mb:.1f}/{total_mb:.1f} MB  •  "
                                        f"⚡ {speed_kb:.0f} KB/s\n"
                                        f"📊 {pct:.1f}%  •  "
                                        f"⏱ ETA: {eta_m}:{eta_s:02d}"
                                    )
                                else:
                                    await progress_cb(
                                        f"📥 **Downloading...**\n"
                                        f"💾 {dl_mb:.1f} MB  •  "
                                        f"⚡ {speed_kb:.0f} KB/s"
                                    )

            size = os.path.getsize(filepath)
            if size < 1024:
                _cleanup_file(filepath)
                return False, f"File too small ({size} bytes)", 0

            logger.info("Direct download complete: %s", _format_size(size))
            return True, "", size

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            logger.warning(
                "Direct download attempt %d/%d failed: %s",
                attempt, MAX_RETRIES, e,
            )
            _cleanup_file(filepath)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    return False, f"Failed after {MAX_RETRIES} attempts", 0


async def _download_with_ytdlp(
    url: str,
    format_id: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback],
) -> Tuple[bool, str, int]:
    """دانلود با yt-dlp (fallback)."""
    if not _check_ytdlp():
        return False, "yt-dlp not installed", 0

    format_selector = format_id if format_id else "best"

    cmd = [
        "yt-dlp",
        "--no-warnings", "--no-playlist",
        "--format", format_selector,
        "--output", filepath,
        "--user-agent", _USER_AGENT,
        "--max-filesize", str(MAX_DOWNLOAD_SIZE),
        "--retries", str(MAX_RETRIES),
        "--progress", "--newline",
        url,
    ]

    error_msg = "Unknown error"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            last_update = 0.0
            while True:
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=120,
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    break

                if not line:
                    break

                text = line.decode(errors="replace").strip()
                if progress_cb and "[download]" in text:
                    now = time.time()
                    if now - last_update >= 2.0:
                        last_update = now
                        pct_m = re.search(r"(\d+\.?\d*)%", text)
                        if pct_m:
                            pct = pct_m.group(1)
                            try:
                                filled = int(float(pct) / 5)
                                bar = "█" * filled + "░" * (20 - filled)
                            except (ValueError, TypeError):
                                bar = "░" * 20
                            await progress_cb(
                                f"📥 **Downloading...**\n"
                                f"`[{bar}]`\n📊 {pct}%\n`{text[:80]}`"
                            )

            stderr_text = ""
            try:
                stderr_data = await asyncio.wait_for(
                    process.stderr.read(), timeout=10,
                )
                stderr_text = stderr_data.decode(errors="replace")
            except asyncio.TimeoutError:
                pass

            await process.wait()

            if process.returncode == 0:
                actual = _find_output_file(filepath)
                if actual:
                    size = os.path.getsize(actual)
                    if size < 1024:
                        _cleanup_file(actual)
                        return False, f"File too small ({size} bytes)", 0
                    if actual != filepath:
                        try:
                            os.rename(actual, filepath)
                        except OSError:
                            pass
                    logger.info("yt-dlp download complete: %s", _format_size(size))
                    return True, "", size
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    continue
                return False, "Output file not found", 0

            # استخراج ارور
            for line in stderr_text.splitlines():
                if "ERROR:" in line:
                    error_msg = line.strip()[6:].strip()[:200]
                    break

            if attempt < MAX_RETRIES:
                _cleanup_file(filepath)
                await asyncio.sleep(RETRY_DELAY * attempt)

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            error_msg = str(e)[:200]
            if attempt < MAX_RETRIES:
                _cleanup_file(filepath)
                await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)
    return False, f"Failed: {error_msg}", 0


def _find_output_file(filepath: str) -> Optional[str]:
    if os.path.exists(filepath):
        return filepath
    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        c = base + ext
        if os.path.exists(c):
            return c
    return None


# ─── Wrappers (سازگاری با bot) ─────────────────────────────


async def download_cartoonporn_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    return await download_cartoonporn_video(
        url, video_url, filepath, "direct", "", progress_cb,
    )


async def download_cartoonporn_m3u8(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    format_id: str = "best",
) -> Tuple[bool, str, int]:
    return await download_cartoonporn_video(
        url, "", filepath, "ytdlp", format_id, progress_cb,
    )
