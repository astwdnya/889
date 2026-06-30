"""
sexvid_handler.py
-----------------
استخراج لینک‌های دانلود از sexvid.xxx (پلتفرم KVS / Kernel Video Sharing).

روش کار:
  - flashvars شامل video_url* با پیشوند 'function/0/' و license_code هست
  - مسیر get_file با الگوریتم رسمی KVS (پورت‌شده از yt-dlp) رمزگشایی میشه
  - چند کیفیت: 480p / 720p / ...
"""

import asyncio
import logging
import os
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("SexvidHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
MAX_RETRIES = 3
RETRY_DELAY = 2.0

_SITE_DOMAIN = "sexvid.xxx"
_SITE_URL = "https://www.sexvid.xxx"
_SITE_REFERER = f"{_SITE_URL}/"

_ALLOWED_HOSTS = frozenset(
    {
        "sexvid.xxx",
        "www.sexvid.xxx",
    }
)

_ALLOWED_HOST_SUFFIXES = (".sexvid.xxx",)

ProgressCallback = Callable[[str], Awaitable[None]]

sexvid_sessions: dict = {}


# ─── Utility ────────────────────────────────────────────────


def is_sexvid_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(f".{_SITE_DOMAIN}")
    except Exception:
        return False


def _is_allowed_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or any(
            host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES
        )
    except Exception:
        return False


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _quality_sort_key(q: dict) -> int:
    nums = re.findall(r"\d+", q["label"])
    return int(nums[-1]) if nums else 0


def _format_progress(
    downloaded: int,
    content_length: int,
    start_time: float,
    now: float,
) -> str:
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
        f"📥 **Downloading...**\n💾 {dl_mb:.1f} MB"
        f"  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
    )


def _check_impersonation_support() -> bool:
    try:
        import curl_cffi  # noqa: F401

        return True
    except ImportError:
        return False


# ─── KVS decoding (الگوریتم رسمی yt-dlp) ────────────────────


def _kvs_get_license_token(license_code: str) -> List[int]:
    """ساخت آرایه‌ی توکن از license_code (پورت دقیق از yt-dlp)."""
    license_code = license_code.replace("$", "")
    license_values = [int(char) for char in license_code]

    modlicense = license_code.replace("0", "1")
    center = len(modlicense) // 2
    fronthalf = int(modlicense[: center + 1])
    backhalf = int(modlicense[center:])
    modlicense = str(4 * abs(fronthalf - backhalf))[: center + 1]

    return [
        (license_values[index + offset] + current) % 10
        for index, current in enumerate(map(int, modlicense))
        for offset in range(4)
    ]


def _kvs_get_real_url(video_url: str, license_code: str) -> str:
    """رمزگشایی video_url با الگوریتم رسمی KVS (پورت دقیق از yt-dlp)."""
    if not video_url.startswith("function/0/"):
        return video_url

    parsed = urllib.parse.urlparse(video_url[len("function/0/") :])
    license_token = _kvs_get_license_token(license_code)
    urlparts = parsed.path.split("/")

    hash_length = 32
    hash_ = urlparts[3][:hash_length]
    indices = list(range(hash_length))

    accum = 0
    for src in reversed(range(hash_length)):
        accum += license_token[src]
        dest = (src + accum) % hash_length
        indices[src], indices[dest] = indices[dest], indices[src]

    urlparts[3] = "".join(hash_[index] for index in indices) + urlparts[3][hash_length:]
    return urllib.parse.urlunparse(parsed._replace(path="/".join(urlparts)))


# ─── HTTP helpers ───────────────────────────────────────────


@asynccontextmanager
async def _get_session(timeout: Optional[ClientTimeout] = None):
    t = timeout or ClientTimeout(total=30, connect=10)
    jar = aiohttp.CookieJar()
    session = aiohttp.ClientSession(timeout=t, headers=_DEFAULT_HEADERS, cookie_jar=jar)
    try:
        yield session
    finally:
        await session.close()


async def _fetch_page(url: str) -> Tuple[Optional[str], int]:
    """دریافت HTML - اول curl_cffi بعد aiohttp."""
    if _check_impersonation_support():
        try:
            from curl_cffi.requests import AsyncSession

            async with AsyncSession() as session:
                try:
                    await session.get(_SITE_REFERER, impersonate="chrome", timeout=15)
                except Exception:
                    pass
                resp = await session.get(
                    url,
                    impersonate="chrome",
                    headers={"Referer": _SITE_REFERER},
                    timeout=25,
                )
                if resp.status_code == 200:
                    return resp.text, 200
                logger.debug("curl_cffi status %d", resp.status_code)
        except Exception as e:
            logger.debug("curl_cffi failed: %s", e)

    try:
        async with _get_session() as session:
            async with session.get(
                url,
                headers={**_DEFAULT_HEADERS, "Referer": _SITE_REFERER},
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    return await resp.text(errors="replace"), 200
                return None, resp.status
    except Exception as e:
        logger.debug("aiohttp failed: %s", e)
        return None, 0


# ─── flashvars parsing ──────────────────────────────────────


def _parse_flashvars(html: str) -> dict:
    """
    استخراج فیلدهای flashvars.
    هر کلید video_* و license_code رو مستقیم از HTML می‌گیریم
    (روش امن که با whitespace زیاد و قطع شدن بلاک مشکل نداره).
    """
    result = {}
    wanted = [
        "license_code",
        "video_id",
        "video_url",
        "video_url_text",
        "video_alt_url",
        "video_alt_url_text",
        "video_alt_url2",
        "video_alt_url2_text",
        "video_alt_url3",
        "video_alt_url3_text",
        "video_alt_url4",
        "video_alt_url4_text",
    ]
    for key in wanted:
        m = re.search(r"\b" + re.escape(key) + r"\s*:\s*'([^']*)'", html)
        if m:
            result[key] = m.group(1)
    return result


def _extract_title(html: str, flashvars: dict) -> str:
    m = re.search(
        r'<meta[^>]+og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
    )
    if m:
        return m.group(1).strip()
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*[Ss]exvid.*$", "", title).strip()
        return title or "Untitled"
    return f"sexvid_{flashvars.get('video_id', 'video')}"


# ─── Main extraction ───────────────────────────────────────


async def extract_sexvid_qualities(url: str) -> Tuple[List[dict], str]:
    """لینک‌های کیفیت مختلف رو از صفحه sexvid.xxx استخراج میکنه."""
    if not is_sexvid_url(url):
        return [], "Invalid URL"

    logger.info("Fetching page: %s", url)
    html, status = await _fetch_page(url)
    if not html:
        return [], f"Could not fetch page (HTTP {status})"

    flashvars = _parse_flashvars(html)
    if not flashvars:
        return [], "flashvars not found (page structure changed?)"

    title = _extract_title(html, flashvars)
    license_code = flashvars.get("license_code", "")

    qualities: List[dict] = []
    seen = set()

    quality_keys = [
        ("video_url", flashvars.get("video_url_text", "720p")),
        ("video_alt_url", flashvars.get("video_alt_url_text", "480p")),
        ("video_alt_url2", flashvars.get("video_alt_url2_text", "360p")),
        ("video_alt_url3", flashvars.get("video_alt_url3_text", "1080p")),
        ("video_alt_url4", flashvars.get("video_alt_url4_text", "2160p")),
    ]

    for key, label in quality_keys:
        raw = flashvars.get(key, "")
        if not raw:
            continue

        decoded = raw.replace("\\/", "/").strip()
        if decoded.startswith("function/0/"):
            if not license_code:
                logger.debug("Encrypted url but no license_code for %s", key)
                continue
            try:
                decoded = _kvs_get_real_url(decoded, license_code)
            except Exception as e:
                logger.warning("KVS decode failed for %s: %s", key, e)
                continue

        if not decoded.startswith("http") or decoded in seen:
            continue
        if not _is_allowed_host(decoded):
            logger.debug("Skipping non-allowed host: %s", decoded[:60])
            continue
        seen.add(decoded)

        qualities.append(
            {
                "label": f"🎥 {label}",
                "url": decoded,
                "method": "direct",
            }
        )

    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info("Extracted %d qualities for: %s", len(qualities), title[:60])
        return qualities, title

    return [], "no video sources found in flashvars"


# ─── Download ───────────────────────────────────────────────

# ─── Download: Multi-segment (fast) ────────────────────────


async def _download_multi_segment(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    num_segments: int = 8,
) -> Tuple[bool, str, int]:
    """
    دانلود چند تیکه‌ای با چند connection همزمان.
    هر تیکه یه Range request جدا میزنه → سرعت N برابر.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        async with AsyncSession() as session:
            head_resp = await session.head(
                url,
                impersonate="chrome",
                headers={"Referer": _SITE_REFERER},
                allow_redirects=True,
                timeout=15,
            )

            content_length = int(head_resp.headers.get("Content-Length", 0))
            accept_ranges = head_resp.headers.get("Accept-Ranges", "")

            if content_length == 0:
                return False, "Cannot determine file size", 0

            if content_length > MAX_DOWNLOAD_SIZE:
                return (
                    False,
                    (f"File too large: {content_length / 1024 / 1024:.0f} MB"),
                    0,
                )

            if accept_ranges.lower() != "bytes":
                logger.info("Server doesn't support Range requests, falling back")
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
            for attempt in range(MAX_RETRIES):
                try:
                    async with AsyncSession() as session:
                        resp = await session.get(
                            url,
                            impersonate="chrome",
                            headers={
                                "Referer": _SITE_REFERER,
                                "Range": f"bytes={byte_start}-{byte_end}",
                                "Accept": "*/*",
                                "Accept-Language": "en-US,en;q=0.9",
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
                                    if now - last_update[0] >= 2.0:
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
                        seg_idx,
                        attempt + 1,
                        e,
                    )
                    _cleanup_file(seg_file)
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2.0 * (attempt + 1))

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

        if abs(final_size - content_length) > 1024:
            logger.warning(
                "Size mismatch: expected %d, got %d",
                content_length,
                final_size,
            )

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


async def _download_with_curl_cffi(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        await progress_cb("📥 **شروع دانلود...**")
        async with AsyncSession() as session:
            resp = await session.get(
                url,
                impersonate="chrome",
                headers={"Referer": _SITE_REFERER, "Accept": "*/*"},
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

            ct = resp.headers.get("Content-Type", "").lower()
            if "text/html" in ct:
                return False, "Response is not a video file", 0

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
                    if now - last_update >= 2.0:
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

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.warning("curl_cffi download error: %s", e)
        return False, str(e)[:150], 0


async def _download_with_aiohttp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    headers = {**_DEFAULT_HEADERS, "Referer": _SITE_REFERER}
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
                            return False, error, 0
                        continue

                    ct = resp.headers.get("Content-Type", "").lower()
                    if "text/html" in ct:
                        return False, "Response is not a video file", 0

                    content_length = int(resp.headers.get("Content-Length", 0))
                    if content_length > MAX_DOWNLOAD_SIZE:
                        return False, "File too large", 0

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
                            if now - last_update >= 2.0:
                                last_update = now
                                await progress_cb(
                                    _format_progress(
                                        downloaded, content_length, start_time, now
                                    )
                                )

                    return True, "", os.path.getsize(filepath)

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            error = str(e)[:150]

        if attempt < MAX_RETRIES:
            _cleanup_file(filepath)
            await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)
    return False, error, 0


async def download_sexvid_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    return False, "sexvid.xxx does not use m3u8 streams", 0


async def download_sexvid_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود لینک مستقیم MP4 از sexvid.xxx.

    اول multi-segment، بعد curl_cffi، آخر aiohttp.
    """
    if not _is_allowed_host(url):
        return False, "URL host not allowed", 0

    # ── روش 1: دانلود چند تیکه‌ای (سریع‌ترین) ──
    if _check_impersonation_support():
        logger.info("Download attempt 1: multi-segment (8 connections)")
        success, error, size = await _download_multi_segment(
            url, filepath, progress_cb, num_segments=8
        )
        if success:
            return True, "", size
        logger.info("Multi-segment failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 2: curl_cffi ──
    if _check_impersonation_support():
        logger.info("Trying download with curl_cffi: %s", url[:80])
        success, error, size = await _download_with_curl_cffi(
            url, filepath, progress_cb
        )
        if success:
            return True, "", size
        logger.info("curl_cffi download failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 3: aiohttp ──
    logger.info("Trying download with aiohttp: %s", url[:80])
    success, error, size = await _download_with_aiohttp(url, filepath, progress_cb)
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0
