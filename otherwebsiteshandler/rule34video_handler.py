"""
rule34video_handler.py
──────────────────────
استخراج و دانلود ویدیو از rule34video.com (KVS Player)

ویژگی‌ها:
  - استخراج کیفیت‌های مختلف (360p, 480p, 720p, 1080p) از flashvars
  - fallback به download links و JSON-LD
  - fallback نهایی به yt-dlp
  - retry و progress callback
"""

import asyncio
import html as html_lib
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

logger = logging.getLogger("Rule34VideoHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_RETRIES = 3
RETRY_DELAY = 2.0
YTDLP_INFO_TIMEOUT = 90
MIN_FILE_SIZE = 1024  # 1 KB

_ALLOWED_HOSTS = frozenset({
    "rule34video.com",
    "www.rule34video.com",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_rule34video_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به rule34video.com هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS
    except Exception:
        return False


def _cleanup_file(filepath: str) -> None:
    """حذف فایل."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Cleaned up: %s", filepath)
    except OSError as e:
        logger.warning("Cleanup failed %s: %s", filepath, e)


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


# ─── HTTP ───────────────────────────────────────────────────


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
                    logger.warning(
                        "HTTP %d (attempt %d/%d)", resp.status, attempt, MAX_RETRIES
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Fetch attempt %d/%d: %s", attempt, MAX_RETRIES, e)

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None, 0


# ─── Extraction ─────────────────────────────────────────────


def _extract_from_html(html: str, page_url: str) -> Tuple[List[dict], str]:
    """
    استخراج کیفیت‌ها و عنوان از HTML.

    سه روش:
      1. flashvars (video_url, video_alt_url, video_alt_url2, video_alt_url3)
      2. download links (a.tag_item_download)
      3. JSON-LD (contentUrl)
    """
    # عنوان
    title = _extract_title(html)

    qualities: List[dict] = []
    seen_urls = set()

    # روش 1: flashvars
    _extract_flashvars(html, qualities, seen_urls)

    # روش 2: download links
    _extract_download_links(html, qualities, seen_urls)

    # روش 3: JSON-LD
    _extract_jsonld(html, qualities, seen_urls)

    # مرتب‌سازی بر اساس کیفیت (بالاترین اول)
    qualities.sort(key=lambda q: q.get("height", 0), reverse=True)

    return qualities, title


def _extract_title(html: str) -> str:
    """استخراج عنوان ویدیو."""
    # JSON-LD name
    ld_m = re.search(r'"name"\s*:\s*"([^"]+)"', html)
    if ld_m:
        return html_lib.unescape(ld_m.group(1).strip())

    # flashvars video_title
    ft_m = re.search(r"video_title\s*:\s*'([^']+)'", html)
    if ft_m:
        return html_lib.unescape(ft_m.group(1).strip())

    # og:title
    og_m = re.search(
        r'property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.I
    )
    if og_m:
        return html_lib.unescape(og_m.group(1).strip())

    # <h1>
    h1_m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    if h1_m:
        return html_lib.unescape(h1_m.group(1).strip())

    # <title>
    t_m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if t_m:
        title = t_m.group(1).strip()
        title = re.sub(r"\s*[-|].*$", "", title).strip() or title
        return html_lib.unescape(title)

    return "Untitled"


def _extract_flashvars(
    html: str, qualities: List[dict], seen_urls: set
) -> None:
    """
    استخراج از flashvars KVS Player.

    ساختار:
      video_url: 'URL',
      video_url_text: '360p',
      video_alt_url: 'URL',
      video_alt_url_text: '480p',
      video_alt_url2: 'URL',
      video_alt_url2_text: '720p',
      video_alt_url3: 'URL',
      video_alt_url3_text: '1080p',
    """
    # جفت‌های url/text
    url_keys = [
        ("video_url", "video_url_text"),
        ("video_alt_url", "video_alt_url_text"),
        ("video_alt_url2", "video_alt_url2_text"),
        ("video_alt_url3", "video_alt_url3_text"),
    ]

    for url_key, text_key in url_keys:
        # استخراج URL
        url_m = re.search(
            rf"{url_key}\s*:\s*'([^']+)'", html
        )
        if not url_m:
            continue

        video_url = url_m.group(1).strip()
        if not video_url or video_url in seen_urls:
            continue
        if "preview" in video_url.lower():
            continue

        seen_urls.add(video_url)

        # استخراج label
        text_m = re.search(rf"{text_key}\s*:\s*'([^']+)'", html)
        label_text = text_m.group(1).strip() if text_m else ""

        height = _parse_height(label_text) or _parse_height(video_url)

        label = f"📺 {height}p (MP4)" if height else f"📺 {label_text or 'MP4'}"

        qualities.append({
            "label": label,
            "url": video_url,
            "method": "direct",
            "height": height or 0,
        })


def _extract_download_links(
    html: str, qualities: List[dict], seen_urls: set
) -> None:
    """استخراج از لینک‌های دانلود (a.tag_item_download)."""
    pattern = re.compile(
        r'<a[^>]+class="[^"]*tag_item_download[^"]*"[^>]+href="([^"]+)"[^>]*>'
        r'\s*(.*?)\s*</a>',
        re.DOTALL | re.I,
    )

    for m in pattern.finditer(html):
        url = m.group(1).strip()
        text = re.sub(r"<[^>]+>", "", m.group(2)).strip()

        # اضافه کردن &download=1 اگه نداره
        if "&download" not in url:
            url_base = url.split("&download")[0]
        else:
            url_base = url.split("&download")[0]

        if url_base in seen_urls:
            continue
        seen_urls.add(url_base)

        height = _parse_height(text) or _parse_height(url)
        label = f"📥 {height}p (Download)" if height else f"📥 {text or 'MP4'}"

        qualities.append({
            "label": label,
            "url": url,
            "method": "direct",
            "height": height or 0,
        })


def _extract_jsonld(
    html: str, qualities: List[dict], seen_urls: set
) -> None:
    """استخراج از JSON-LD."""
    ld_pattern = re.compile(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        re.DOTALL,
    )

    for m in ld_pattern.finditer(html):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue

        content_url = data.get("contentUrl", "")
        if not content_url or content_url in seen_urls:
            continue

        seen_urls.add(content_url)
        height = _parse_height(content_url)

        qualities.append({
            "label": f"📺 {height}p (JSON-LD)" if height else "📺 MP4 (JSON-LD)",
            "url": content_url,
            "method": "direct",
            "height": height or 360,
        })


def _parse_height(text: str) -> Optional[int]:
    """استخراج ارتفاع (کیفیت) از متن یا URL."""
    if not text:
        return None
    m = re.search(r"(\d{3,4})p", text)
    if m:
        return int(m.group(1))
    m = re.search(r"_(\d{3,4})\.", text)
    if m:
        return int(m.group(1))
    return None


# ─── yt-dlp fallback ───────────────────────────────────────


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
            direct_url = fmt.get("url", "")
            if not direct_url:
                continue

            height = fmt.get("height")
            format_id = fmt.get("format_id", "")
            resolution = fmt.get("resolution", "")

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


async def extract_rule34video_qualities(
    url: str,
) -> Tuple[List[dict], str]:
    """
    استخراج کیفیت‌های موجود.

    اول HTML (flashvars + download links + JSON-LD)، بعد yt-dlp.
    """
    if not is_rule34video_url(url):
        return [], "Invalid URL"

    # روش 1: HTML مستقیم
    html, status = await _fetch_page(url)
    if html:
        qualities, title = _extract_from_html(html, url)
        if qualities:
            logger.info(
                "Extracted %d qualities from HTML: %s",
                len(qualities), title[:60],
            )
            return qualities, title
        logger.info("No qualities from HTML, trying yt-dlp")

    # روش 2: yt-dlp
    qualities, title = await _extract_with_ytdlp(url)
    if qualities:
        logger.info(
            "Extracted %d qualities from yt-dlp: %s",
            len(qualities), title[:60],
        )
    return qualities, title


# ─── Download ───────────────────────────────────────────────


async def download_rule34video(
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
        video_url: لینک مستقیم ویدیو
        filepath: مسیر فایل خروجی
        method: "direct" یا "ytdlp"
        format_id: شناسه فرمت (برای ytdlp)
        progress_cb: callback پیشرفت
    """
    if not is_rule34video_url(url):
        return False, "URL not allowed", 0

    if method == "direct" and video_url:
        success, error, size = await _download_direct(
            video_url, url, filepath, progress_cb,
        )
        if success:
            return True, "", size
        logger.info("Direct failed: %s, trying yt-dlp", error)

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
                async with session.get(video_url, allow_redirects=True) as resp:
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
                        return (
                            False,
                            f"Too large: {_format_size(content_length)}",
                            0,
                        )

                    total_mb = (
                        content_length / 1024 / 1024 if content_length else 0
                    )
                    downloaded = 0
                    start_time = time.time()
                    last_update = 0.0

                    async with aiofiles.open(filepath, "wb") as f:
                        async for chunk in resp.content.iter_chunked(
                            256 * 1024
                        ):
                            await f.write(chunk)
                            downloaded += len(chunk)

                            if downloaded > MAX_DOWNLOAD_SIZE:
                                _cleanup_file(filepath)
                                return False, "Exceeded size limit", 0

                            now = time.time()
                            if progress_cb and now - last_update >= 2.0:
                                last_update = now
                                await _report_progress(
                                    progress_cb,
                                    downloaded,
                                    content_length,
                                    total_mb,
                                    start_time,
                                )

            size = os.path.getsize(filepath)
            if size < MIN_FILE_SIZE:
                _cleanup_file(filepath)
                return False, f"Too small ({size} bytes)", 0

            logger.info("Download complete: %s", _format_size(size))
            return True, "", size

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            logger.warning(
                "Download attempt %d/%d: %s", attempt, MAX_RETRIES, e
            )
            _cleanup_file(filepath)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    return False, f"Failed after {MAX_RETRIES} attempts", 0


async def _report_progress(
    progress_cb: ProgressCallback,
    downloaded: int,
    content_length: int,
    total_mb: float,
    start_time: float,
) -> None:
    """گزارش پیشرفت دانلود."""
    elapsed = time.time() - start_time
    speed = downloaded / elapsed if elapsed > 0 else 0
    dl_mb = downloaded / 1024 / 1024
    speed_kb = min(speed / 1024, 99999)

    if content_length > 0:
        pct = downloaded / content_length * 100
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        eta_secs = (
            int((content_length - downloaded) / speed) if speed > 0 else 0
        )
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
            f"💾 {dl_mb:.1f} MB  •  ⚡ {speed_kb:.0f} KB/s"
        )


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
                    if size < MIN_FILE_SIZE:
                        _cleanup_file(actual)
                        return False, f"Too small ({size} bytes)", 0
                    if actual != filepath:
                        try:
                            os.rename(actual, filepath)
                        except OSError:
                            pass
                    logger.info(
                        "yt-dlp download complete: %s", _format_size(size)
                    )
                    return True, "", size
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    continue
                return False, "Output file not found", 0

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
    """پیدا کردن فایل خروجی."""
    if os.path.exists(filepath):
        return filepath
    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = base + ext
        if os.path.exists(candidate):
            return candidate
    return None


# ─── Wrappers (سازگاری با bot) ─────────────────────────────


async def download_rule34video_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    """Wrapper دانلود مستقیم."""
    return await download_rule34video(
        url, video_url, filepath, "direct", "", progress_cb,
    )


async def download_rule34video_ytdlp(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    format_id: str = "best",
) -> Tuple[bool, str, int]:
    """Wrapper دانلود با yt-dlp."""
    return await download_rule34video(
        url, "", filepath, "ytdlp", format_id, progress_cb,
    )
