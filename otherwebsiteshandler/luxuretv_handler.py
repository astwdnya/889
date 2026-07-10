"""
luxuretv_handler.py
───────────────────
استخراج و دانلود ویدیو از en.luxuretv.com

روش کار (بر اساس تحلیل واقعی):

  ─── ساختار صفحه ───
  سایت پشت Cloudflare هست. aiohttp و curl_cffi هر دو CF challenge می‌گیرن.
  ولی yt-dlp می‌تونه صفحه رو fetch کنه (با cookie handling خاص).

  URL ویدیو از HTML استخراج می‌شه (با کمک yt-dlp):
    https://en.luxuretv.com/cf-stream/{video_id}/{path}/{hash}.mp4?md5={md5}&expires={timestamp}

  ─── ساختار URL ───
  - /cf-stream/{video_id}/{path}/{hash}.mp4
  - query params: md5 (HMAC signature), expires (Unix timestamp TTL ~12h)
  - فقط یه کیفیت (نه چندتا)

  ─── سرور ───
  - en.luxuretv.com: پشت Cloudflare
  - CDN: cf-stream (روی همون هاست)
  - Accept-Ranges: bytes ✓
  - **نیاز به Referer header** (بدون Referer = 403)

  ─── کوکی ───
  - PHPSESSID, viewed_video_{id}, SERVERID
  - برای CDN لازم نیست (URL امضا شده)، فقط برای page fetch

استراتژی:
  1. استفاده از yt-dlp برای استخراج URL (CF bypass)
  2. multi-segment download با 32 workers (با Referer)
  3. fallback به yt-dlp برای دانلود مستقیم

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

logger = logging.getLogger("LuxureTVHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
CHUNK_SIZE = 1024 * 1024  # 1 MB
MAX_RETRIES = 3
RETRY_DELAY = 2.0
MULTI_SEGMENT_MIN_SIZE = 5 * 1024 * 1024  # 5 MB

MULTI_SEGMENT_WORKERS = 32
MULTI_SEGMENT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB
CONNECTOR_LIMIT = 50
CONNECTOR_LIMIT_PER_HOST = 50

_ALLOWED_HOSTS = frozenset({
    "luxuretv.com",
    "en.luxuretv.com",
    "www.luxuretv.com",
    "media.luxuretv.com",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────────────────────


def is_luxuretv_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".luxuretv.com")
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


# ─── Extraction (via yt-dlp) ──────────────────────────────────────────────


async def _extract_via_ytdlp(url: str) -> Tuple[List[dict], str, dict]:
    """
    استخراج URL ویدیو از صفحه با استفاده از yt-dlp.

    Returns:
        (sources, title, info)
        sources: [{label, url, height, quality_key, method, is_hd}]
    """
    if not shutil.which("yt-dlp") and not _has_ytdlp_module():
        return [], "yt-dlp not installed", {}

    # Use yt-dlp CLI directly (Python API doesn't support --impersonate properly)
    return await _extract_via_ytdlp_cli(url)


def _has_ytdlp_module() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


async def _extract_via_ytdlp_cli(url: str) -> Tuple[List[dict], str, dict]:
    """Fallback: استفاده از yt-dlp CLI."""
    if not shutil.which("yt-dlp"):
        return [], "yt-dlp not installed", {}

    cmd = [
        "yt-dlp", "--no-warnings", "--no-playlist",
        "--dump-json", "--no-download", "--no-check-certificates",
        "--impersonate", "chrome", "--extractor-args", "generic:impersonate",
        url,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        stdout_text = stdout.decode(errors="replace").strip()
        if process.returncode != 0 or not stdout_text:
            return [], "yt-dlp extraction failed", {}

        sources = []
        title = ""
        info = {}

        # Parse JSON lines (yt-dlp may output multiple entries)
        for line in stdout_text.split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            video_url = data.get("url", "")
            if not video_url:
                # Check formats
                formats = data.get("formats", [])
                if formats:
                    video_url = formats[0].get("url", "")

            if video_url and "/cf-stream/" in video_url:
                title = data.get("title", "Untitled")
                title = re.sub(r"\s*-\s*LuxureTV.*$", "", title, flags=re.IGNORECASE)
                sources.append({
                    "label": "📺 MP4 (default)",
                    "url": video_url,
                    "height": 480,
                    "quality_key": "default",
                    "method": "ytdlp_cli",
                    "is_hd": False,
                })
                if data.get("formats"):
                    fmt = data["formats"][0]
                    info["http_headers"] = fmt.get("http_headers", {})
                    info["cookies"] = fmt.get("cookies", "")
                info["thumbnail"] = data.get("thumbnail", "")
                break

        return sources, title, info

    except Exception as e:
        logger.error(f"yt-dlp CLI error: {e}")
        return [], str(e), {}


# ─── Main API: extract qualities ──────────────────────────────────────────


async def extract_luxuretv_qualities(url, progress_cb=None):
    """استخراج کیفیت‌های ویدیو."""
    if not is_luxuretv_url(url):
        return [], "Invalid URL — host not allowed", {}

    if progress_cb:
        await progress_cb("🔄 **استخراج URL ویدیو (با yt-dlp)...**")

    sources, title, info = await _extract_via_ytdlp(url)

    if not sources:
        logger.error("No video sources found")
        return [], title or "URL ویدیو در صفحه پیدا نشد", {}

    logger.info("Found %d video sources", len(sources))

    if progress_cb:
        labels = ", ".join(s["label"] for s in sources)
        await progress_cb(f"✅ **پیدا شد:** {title[:50]}\n🎞 کیفیت‌ها: {labels}")

    return sources, title, {
        "thumbnail": info.get("thumbnail", ""),
        "page_url": url,
        "cookies": {},
        "duration": None,
        "fetch_method": "ytdlp",
        "http_headers": info.get("http_headers", {}),
        "cookie_str": info.get("cookies", ""),
    }


# ─── Download: Multi-segment (fast) ───────────────────────────────────────


active_downloads: dict = {}


async def _download_multi_segment(
    direct_url, filepath, referer, cookies, progress_cb, dl_id="",
    num_workers=MULTI_SEGMENT_WORKERS,
    cookie_str: str = "",
):
    """دانلود چند تیکه‌ای با work-queue pattern."""
    try:
        cdn_headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "same-origin",
            "Referer": referer,
        }
        # Add cookie string if provided
        if cookie_str:
            cdn_headers["Cookie"] = cookie_str

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
                    elif r.status == 403:
                        return False, "HTTP_403", 0
        except Exception as e:
            logger.warning(f"HEAD request failed: {e}")

        # ─── 2. probe با Range ───
        if content_length == 0:
            try:
                timeout = ClientTimeout(total=10, connect=5)
                probe_headers = dict(cdn_headers)
                probe_headers["Range"] = "bytes=0-1023"
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
        await progress_cb(
            f"📥 **Downloading...**\n💾 Size: {total_mb:.1f} MB\n🔥 {num_workers} parallel workers"
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
        logger.info("[DL-LX] Work-queue: %d chunks, %d workers, total=%d", total_chunks, num_workers, content_length)

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
                                raise Exception(f"Size mismatch: expected {chunk_size}, got {len(chunk_data)}")
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
                        logger.warning("[DL-LX] W%d c%d attempt %d failed: %s", worker_id, c_idx, attempt + 1, e)
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
                logger.warning("[DL-LX] %d worker failures, %d chunk failures", len(real_failures), len(failed_chunks))
                _cleanup_file(filepath)
                return False, f"Multi-segment failed: {len(real_failures)+len(failed_chunks)} chunks", 0

        except Exception as e:
            logger.error(f"[DL-LX] Work-queue error: {e}", exc_info=True)
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
        logger.info("[DL-LX] Multi-segment DONE | size=%s | time=%.1fs | avg_speed=%.1f MB/s",
                     _format_size(file_size), elapsed, avg_speed)
        return True, "", file_size

    except Exception as e:
        logger.error(f"[DL-LX] Multi-segment error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Download: Single connection (fallback) ───────────────────────────────


async def _download_single_aiohttp(url, filepath, referer, cookies, progress_cb, dl_id="", cookie_str=""):
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
    if cookie_str:
        headers["Cookie"] = cookie_str

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
                                    await progress_cb(_format_progress(downloaded, content_length, start_time, now))
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


async def _download_with_ytdlp(url, filepath, progress_cb):
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0
    await progress_cb("📥 **Fallback: yt-dlp direct download...**")
    try:
        cmd = [
            "yt-dlp", "--no-warnings", "--progress", "--newline",
            "--no-check-certificates", "-f", "best",
            "--concurrent-fragments", "16",
            "--retries", "10", "--fragment-retries", "10",
            "--buffer-size", "16K",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "-o", filepath,
            "--impersonate", "chrome",
            url,
        ]
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
                    pct_match = re.search(r"(\d+\.?\d*)%", text)
                    if pct_match:
                        pct = pct_match.group(1)
                        try:
                            pct_num = float(pct)
                            filled = int(pct_num / 5)
                            bar = "█" * filled + "░" * (20 - filled)
                        except (ValueError, TypeError):
                            bar = "░" * 20
                        await progress_cb(f"📥 **Downloading (yt-dlp)...**\n`[{bar}]`\n📊 {pct}%")
        await process.wait()
        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            return False, stderr[-200:], 0
        actual_path = filepath
        if not os.path.exists(actual_path):
            base, _ = os.path.splitext(filepath)
            for ext in (".mp4", ".mkv", ".webm", ".ts"):
                candidate = base + ext
                if os.path.exists(candidate):
                    actual_path = candidate
                    break
        if not os.path.exists(actual_path):
            return False, "Output file not found", 0
        size = os.path.getsize(actual_path)
        if size < MIN_VALID_VIDEO_SIZE:
            _cleanup_file(actual_path)
            return False, f"File too small ({size} bytes)", 0
        if actual_path != filepath:
            try:
                os.rename(actual_path, filepath)
            except OSError:
                pass
        logger.info(f"[DL-LX] yt-dlp DONE | size={_format_size(size)}")
        return True, "", size
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.error(f"[DL-LX] yt-dlp error: {e}", exc_info=True)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


# ─── Public API ────────────────────────────────────────────────────────────


async def download_luxuretv_video(
    page_url, video_url, filepath, progress_cb=None, cookies=None, dl_id="",
    quality_key="", cookie_str="",
):
    """دانلود ویدیو از luxuretv."""
    if not is_luxuretv_url(page_url):
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
    logger.info(f"[DL-LX] Attempt 1: multi-segment ({MULTI_SEGMENT_WORKERS} workers)")
    success, error, size = await _download_multi_segment(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
        cookie_str=cookie_str,
    )
    if success:
        return True, "", size
    if error == "Cancelled by user":
        return False, error, 0
    if error == "HTTP_403":
        logger.info("[DL-LX] 403, refreshing...")
        if progress_cb:
            await progress_cb("🔄 **Refreshing session...**")
        try:
            new_sources, _, new_info = await extract_luxuretv_qualities(page_url, progress_cb=None)
            if new_sources:
                video_url = new_sources[0]["url"]
                cookie_str = new_info.get("cookie_str", "")
        except Exception as e:
            logger.warning(f"[DL-LX] refresh failed: {e}")

        success, error, size = await _download_multi_segment(
            video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
            cookie_str=cookie_str,
        )
        if success:
            return True, "", size
    logger.info(f"[DL-LX] Multi-segment failed: {error}")
    _cleanup_file(filepath)

    # ── روش 2: single-connection ──
    logger.info("[DL-LX] Attempt 2: single-connection")
    success, error, size = await _download_single_aiohttp(
        video_url, filepath, referer, cookies, progress_cb, dl_id=dl_id,
        cookie_str=cookie_str,
    )
    if success:
        return True, "", size
    logger.info(f"[DL-LX] Single failed: {error}")
    _cleanup_file(filepath)

    # ─ـ روش 3: yt-dlp on direct video URL ──
    logger.info("[DL-LX] Attempt 3: yt-dlp on direct URL")
    success, error, size = await _download_with_ytdlp(video_url, filepath, progress_cb)
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error or "All download methods failed", 0


async def download_luxuretv_direct(
    url, filepath, progress_cb=None, video_url="", quality="high", dl_id="",
):
    """Wrapper برای سازگاری با bot architecture."""
    if not video_url:
        qualities, title, info = await extract_luxuretv_qualities(url, progress_cb)
        if not qualities:
            return False, title or "Extraction failed", 0
        selected = qualities[0]  # luxuretv فقط یه کیفیت داره
        video_url = selected["url"]
        quality_key = selected.get("quality_key", "")
        cookies = info.get("cookies", {})
        cookie_str = info.get("cookie_str", "")
    else:
        qualities, title, info = await extract_luxuretv_qualities(url, progress_cb)
        cookies = info.get("cookies", {}) if info else {}
        cookie_str = info.get("cookie_str", "") if info else ""
        quality_key = quality

    return await download_luxuretv_video(
        url, video_url, filepath, progress_cb,
        cookies=cookies, dl_id=dl_id, quality_key=quality_key,
        cookie_str=cookie_str,
    )


# ─── Self-test ─────────────────────────────────────────────────────────────


async def _self_test():
    """تست خودي هندلر."""
    test_url = "https://en.luxuretv.com/videos/real-brother-and-sister-incest-133097.html"
    print(f"\n{'═' * 80}")
    print(f"Self-test: {test_url}")
    print(f"{'═' * 80}\n")

    async def progress(msg):
        print(f"  → {msg}")

    sources, title, info = await extract_luxuretv_qualities(test_url, progress_cb=progress)

    print(f"\n  Title: {title}")
    print(f"  Thumbnail: {info.get('thumbnail', '')[:120]}")
    print(f"  Cookie str: {info.get('cookie_str', '')[:80]}")
    print(f"\n  Sources ({len(sources)}):")
    for s in sources:
        print(f"    [{s['quality_key']:6s}] {s['url'][:120]} ({s['method']})")

    return sources, title, info


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_self_test())
