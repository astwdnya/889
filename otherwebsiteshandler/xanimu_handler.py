"""
xanimu_handler.py
─────────────────
استخراج و دانلود ویدیو از xanimu.com

ویژگی‌ها:
  - bypass Cloudflare با subprocess curl
  - استخراج لینک‌های MP4 از HTML (video tag + JS vars)
  - دو کیفیت: High و Low
  - verify token دار (time-limited)
"""

import asyncio
import html as html_lib
import json
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse, unquote

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("XanimuHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_RETRIES = 3
RETRY_DELAY = 2.0
CURL_TIMEOUT = 30
MIN_FILE_SIZE = 1024  # 1 KB

_ALLOWED_HOSTS = frozenset({
    "xanimu.com",
    "www.xanimu.com",
})

# CDN hosts مجاز برای دانلود
_CDN_HOSTS = frozenset({
    "xcdn1.nosofiles.com",
    "xcdn2.nosofiles.com",
    "st1.nosofiles.com",
    "st2.nosofiles.com",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_xanimu_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به xanimu.com هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS
    except Exception:
        return False


def _is_valid_cdn_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به CDN مجاز هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _CDN_HOSTS or host in _ALLOWED_HOSTS
    except Exception:
        return False


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Cleaned up: %s", filepath)
    except OSError as e:
        logger.warning("Cleanup failed %s: %s", filepath, e)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def _check_curl() -> bool:
    return shutil.which("curl") is not None


# ─── HTTP: curl subprocess (Cloudflare bypass) ─────────────


async def _fetch_with_curl(url: str) -> Tuple[Optional[str], int]:
    """
    دریافت HTML با curl سیستم.
    Cloudflare فقط TLS fingerprint پایتون رو بلاک می‌کنه،
    curl سیستم bypass میشه.
    """
    if not _check_curl():
        logger.error("curl not found in PATH")
        return None, 0

    cmd = [
        "curl", "-s",
        "-w", "\n__HTTP_CODE__%{http_code}",
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-L",  # follow redirects
        "--compressed",
        "--max-time", str(CURL_TIMEOUT),
        url,
    ]

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=CURL_TIMEOUT + 10,
            )

            output = stdout.decode(errors="replace")

            # جدا کردن HTTP code از body
            code_marker = "__HTTP_CODE__"
            if code_marker in output:
                parts = output.rsplit(code_marker, 1)
                html = parts[0]
                try:
                    status = int(parts[1].strip())
                except (ValueError, IndexError):
                    status = 0
            else:
                html = output
                status = 200 if process.returncode == 0 else 0

            if status == 200 and len(html) > 1000:
                return html, 200

            if 400 <= status < 500:
                logger.warning("curl HTTP %d for %s", status, url)
                return None, status

            logger.warning(
                "curl attempt %d/%d: status=%d len=%d",
                attempt, MAX_RETRIES, status, len(html),
            )

        except asyncio.TimeoutError:
            logger.warning("curl timeout attempt %d/%d", attempt, MAX_RETRIES)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("curl attempt %d/%d: %s", attempt, MAX_RETRIES, e)

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None, 0


# ─── Extraction ─────────────────────────────────────────────


def _extract_from_html(html: str, page_url: str) -> Tuple[List[dict], str]:
    """
    استخراج کیفیت‌ها و عنوان از HTML.

    سه منبع:
      1. JS vars (videoHigh, videoLow)
      2. <video> tag و <source> tags
      3. MP4 URLs مستقیم در HTML
    """
    title = _extract_title(html)
    qualities: List[dict] = []
    seen_urls = set()

    # روش 1: JS variables
    _extract_js_vars(html, qualities, seen_urls)

    # روش 2: <video> و <source> tags
    _extract_video_tags(html, qualities, seen_urls)

    # روش 3: MP4 URLs مستقیم (فقط CDN)
    _extract_direct_mp4(html, qualities, seen_urls)

    # مرتب‌سازی: high اول
    qualities.sort(key=lambda q: q.get("height", 0), reverse=True)

    return qualities, title


def _extract_title(html: str) -> str:
    """استخراج عنوان ویدیو."""
    # toStore JSON
    ts_m = re.search(r'"title"\s*:\s*"([^"]+)"', html)
    if ts_m:
        title = ts_m.group(1).strip()
        if len(title) > 3:
            return html_lib.unescape(title)

    # <title>
    t_m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if t_m:
        title = t_m.group(1).strip()
        # حذف " - XAnimu.com"
        title = re.sub(r"\s*[-|]\s*XAnimu\.com\s*$", "", title, flags=re.I).strip()
        if title:
            return html_lib.unescape(title)

    # og:title
    og_m = re.search(
        r'property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.I
    )
    if og_m:
        return html_lib.unescape(og_m.group(1).strip())

    # h1
    h1_m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    if h1_m:
        return html_lib.unescape(h1_m.group(1).strip())

    return "Untitled"


def _extract_js_vars(
    html: str, qualities: List[dict], seen_urls: set
) -> None:
    """
    استخراج از متغیرهای JS.

    فرمت:
      var videoHigh="https://xcdn1.nosofiles.com/.../XXX_high.mp4?verify=...";
      var videoHighTitle="720p";
      var videoLow="https://xcdn1.nosofiles.com/.../XXX_low.mp4?verify=...";
      var videoLowTitle="240p";
    """
    # videoHigh
    high_m = re.search(
        r'var\s+videoHigh\s*=\s*"([^"]+)"', html
    )
    if high_m:
        url = high_m.group(1).strip()
        if url and url not in seen_urls and _is_valid_cdn_url(url):
            seen_urls.add(url)

            # عنوان کیفیت
            high_title_m = re.search(
                r'var\s+videoHighTitle\s*=\s*"([^"]+)"', html
            )
            label_text = high_title_m.group(1) if high_title_m else "High"
            height = _parse_height(label_text) or 720

            qualities.append({
                "label": f"📺 {label_text} (High Quality)",
                "url": url,
                "method": "direct",
                "height": height,
                "quality_key": "high",
            })

    # videoLow
    low_m = re.search(
        r'var\s+videoLow\s*=\s*"([^"]+)"', html
    )
    if low_m:
        url = low_m.group(1).strip()
        if url and url not in seen_urls and _is_valid_cdn_url(url):
            seen_urls.add(url)

            low_title_m = re.search(
                r'var\s+videoLowTitle\s*=\s*"([^"]+)"', html
            )
            label_text = low_title_m.group(1) if low_title_m else "Low"
            height = _parse_height(label_text) or 360

            qualities.append({
                "label": f"📺 {label_text} (Low Quality)",
                "url": url,
                "method": "direct",
                "height": height,
                "quality_key": "low",
            })


def _extract_video_tags(
    html: str, qualities: List[dict], seen_urls: set
) -> None:
    """استخراج از <video> و <source> tags."""
    # <video src="...">
    video_src_m = re.search(
        r"<video[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I
    )
    if video_src_m:
        url = html_lib.unescape(video_src_m.group(1).strip())
        if url not in seen_urls and _is_valid_cdn_url(url):
            seen_urls.add(url)
            is_high = "_high" in url
            height = 720 if is_high else 360
            qualities.append({
                "label": f"📺 {'High' if is_high else 'Low'} (Video Tag)",
                "url": url,
                "method": "direct",
                "height": height,
                "quality_key": "high" if is_high else "low",
            })

    # <source src="...">
    for m in re.finditer(
        r"<source[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I
    ):
        url = html_lib.unescape(m.group(1).strip())
        if url in seen_urls or not _is_valid_cdn_url(url):
            continue
        seen_urls.add(url)

        is_high = "_high" in url
        height = 720 if is_high else 360
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} (Source Tag)",
            "url": url,
            "method": "direct",
            "height": height,
            "quality_key": "high" if is_high else "low",
        })


def _extract_direct_mp4(
    html: str, qualities: List[dict], seen_urls: set
) -> None:
    """استخراج MP4 URLs مستقیم از HTML (فقط CDN، بدون trailer/preview)."""
    mp4_pattern = re.compile(
        r'(https?://[^\s"\'<>]*nosofiles\.com/[^\s"\'<>]+\.mp4[^\s"\'<>]*)'
    )

    for m in mp4_pattern.finditer(html):
        url = m.group(1).strip()

        # فیلتر trailer و preview
        if "/trailer.mp4" in url or "preview" in url:
            continue
        if url in seen_urls:
            continue

        seen_urls.add(url)

        is_high = "_high" in url
        height = 720 if is_high else 360
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} (Direct)",
            "url": url,
            "method": "direct",
            "height": height,
            "quality_key": "high" if is_high else "low",
        })


def _parse_height(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d{3,4})p?", text)
    if m:
        return int(m.group(1))
    return None


def _extract_video_info(html: str) -> dict:
    """استخراج اطلاعات اضافی ویدیو (views, duration, likes, poster)."""
    info = {}

    # toStore JSON
    ts_m = re.search(r"const\s+toStore\s*=\s*(\{[^;]+\})", html)
    if ts_m:
        try:
            data = json.loads(ts_m.group(1))
            info["views"] = data.get("views", 0)
            info["duration"] = data.get("length", "")
            info["likes"] = data.get("likes", "")
            info["thumbnail"] = data.get("thumbnail", "")
            info["post_id"] = data.get("postId", 0)
        except (json.JSONDecodeError, ValueError):
            pass

    # poster از video tag
    if "thumbnail" not in info:
        poster_m = re.search(r"poster=[\"']([^\"']+)[\"']", html)
        if poster_m:
            info["thumbnail"] = poster_m.group(1)

    return info


# ─── Main extraction ───────────────────────────────────────


async def extract_xanimu_qualities(
    url: str,
) -> Tuple[List[dict], str, dict]:
    """
    استخراج کیفیت‌های موجود.

    Returns:
        (qualities, title, info)
    """
    if not is_xanimu_url(url):
        return [], "Invalid URL", {}

    html, status = await _fetch_with_curl(url)
    if not html:
        logger.error("Failed to fetch page (status=%d)", status)
        return [], "Failed to fetch page", {}

    # بررسی Cloudflare challenge
    if "Just a moment" in html or len(html) < 2000:
        logger.error("Cloudflare challenge not bypassed")
        return [], "Cloudflare blocked", {}

    qualities, title = _extract_from_html(html, url)
    info = _extract_video_info(html)

    # حذف تکراری بر اساس quality_key
    unique = {}
    for q in qualities:
        key = q.get("quality_key", q.get("url"))
        if key not in unique:
            unique[key] = q
    qualities = sorted(unique.values(), key=lambda q: q.get("height", 0), reverse=True)

    if qualities:
        logger.info(
            "Extracted %d qualities: %s", len(qualities), title[:60]
        )
    else:
        logger.warning("No qualities found for: %s", url)

    return qualities, title, info


# ─── Download ───────────────────────────────────────────────


async def download_xanimu_video(
    url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو از xanimu.

    Args:
        url: آدرس صفحه اصلی
        video_url: لینک مستقیم MP4 (با verify token)
        filepath: مسیر فایل خروجی
        progress_cb: callback پیشرفت
    """
    if not is_xanimu_url(url):
        return False, "URL not allowed", 0

    if not video_url or not _is_valid_cdn_url(video_url):
        return False, "Invalid video URL", 0

    # دانلود مستقیم با aiohttp (CDN بدون Cloudflare هست)
    success, error, size = await _download_direct(
        video_url, url, filepath, progress_cb,
    )
    if success:
        return True, "", size

    # Fallback: دانلود با curl
    logger.info("Direct download failed: %s, trying curl", error)
    return await _download_with_curl(video_url, filepath, progress_cb)


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


async def _download_with_curl(
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback],
) -> Tuple[bool, str, int]:
    """Fallback: دانلود با curl سیستم."""
    if not _check_curl():
        return False, "curl not found", 0

    cmd = [
        "curl", "-L",
        "-o", filepath,
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept: */*",
        "--compressed",
        "--max-time", "3600",
        "--retry", str(MAX_RETRIES),
        "--progress-bar",
        video_url,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if progress_cb:
            await progress_cb("📥 **Downloading with curl...**")

        _, stderr_data = await asyncio.wait_for(
            process.communicate(), timeout=3600,
        )

        if process.returncode == 0 and os.path.exists(filepath):
            size = os.path.getsize(filepath)
            if size < MIN_FILE_SIZE:
                _cleanup_file(filepath)
                return False, f"Too small ({size} bytes)", 0
            logger.info("curl download complete: %s", _format_size(size))
            return True, "", size

        error = stderr_data.decode(errors="replace").strip()[:200]
        _cleanup_file(filepath)
        return False, f"curl failed: {error}", 0

    except asyncio.TimeoutError:
        _cleanup_file(filepath)
        return False, "curl timeout", 0
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


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


# ─── Wrapper (سازگاری با bot) ──────────────────────────────


async def download_xanimu_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    """Wrapper دانلود."""
    return await download_xanimu_video(
        url, video_url, filepath, progress_cb,
    )
