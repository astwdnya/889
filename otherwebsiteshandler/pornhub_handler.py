"""
pornhub_handler.py
──────────────────
استخراج و دانلود ویدیو از PornHub.com

روش کار:
  - استخراج اطلاعات: yt-dlp --dump-json
  - دانلود: yt-dlp با format selector (فقط HTTPS formats)
  - بدون نیاز به ffmpeg (فرمت‌های HTTPS مستقیم MP4 هستن)
  - بدون HTTP مستقیم به صفحه (PornHub بلاک میکنه)
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

logger = logging.getLogger("PornHubHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# حداکثر حجم: 2 گیگابایت
MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

# حداکثر عمر session: 30 دقیقه
SESSION_TTL = 30 * 60

# retry
MAX_RETRIES = 3
RETRY_DELAY = 2.0

# timeout برای yt-dlp (ثانیه)
YTDLP_INFO_TIMEOUT = 90
YTDLP_DOWNLOAD_TIMEOUT = 3600

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset(
    {
        "pornhub.com",
        "www.pornhub.com",
        "m.pornhub.com",
        "pornhub.org",
        "www.pornhub.org",
    }
)

# session های در حال انتظار
pornhub_sessions: Dict[str, dict] = {}

# تایپ callback
ProgressCallback = Callable[[str], Awaitable[None]]

# چک ffmpeg یکبار
_ffmpeg_available: Optional[bool] = None


# ─── Utility ────────────────────────────────────────────────


def is_pornhub_url(url: str) -> bool:
    """بررسی اینکه URL واقعاً مربوط به PornHub هست."""
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
        for sid, data in pornhub_sessions.items()
        if now - data.get("created_at", 0) > SESSION_TTL
    ]
    for sid in expired:
        pornhub_sessions.pop(sid, None)
    if expired:
        logger.info("Cleaned up %d expired pornhub sessions", len(expired))
    return len(expired)


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


def _check_ytdlp() -> bool:
    """بررسی نصب بودن yt-dlp."""
    return shutil.which("yt-dlp") is not None


def _check_ffmpeg() -> bool:
    """بررسی نصب بودن ffmpeg (با cache)."""
    global _ffmpeg_available
    if _ffmpeg_available is None:
        _ffmpeg_available = shutil.which("ffmpeg") is not None
        if not _ffmpeg_available:
            logger.warning("ffmpeg not found - HLS formats will be skipped")
    return _ffmpeg_available


# ─── yt-dlp base command ───────────────────────────────────


def _ytdlp_base_cmd() -> List[str]:
    """ساخت command پایه yt-dlp با flag های لازم."""
    return [
        "yt-dlp",
        "--no-warnings",
        "--no-playlist",
        "--user-agent",
        _USER_AGENT,
    ]


# ─── Extraction ────────────────────────────────────────────


async def _run_ytdlp_json(url: str) -> Tuple[Optional[dict], str]:
    """
    اجرای yt-dlp --dump-json.

    Returns:
        (data_dict, error_message)
    """
    if not _check_ytdlp():
        return None, "yt-dlp is not installed"

    cmd = _ytdlp_base_cmd() + [
        "--dump-json",
        "--no-download",
        url,
    ]

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=YTDLP_INFO_TIMEOUT,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.warning(
                    "yt-dlp info timed out (attempt %d/%d)",
                    attempt,
                    MAX_RETRIES,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
                continue

            if process.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                # استخراج خط ERROR اصلی
                for line in err.splitlines():
                    if "ERROR:" in line:
                        err = line.strip()
                        break
                logger.warning(
                    "yt-dlp info failed (attempt %d/%d, code %d): %s",
                    attempt,
                    MAX_RETRIES,
                    process.returncode,
                    err[:200],
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    continue
                return None, err[:200]

            raw = stdout.decode(errors="replace").strip()
            if not raw:
                return None, "Empty response from yt-dlp"

            data = json.loads(raw)
            return data, ""

        except json.JSONDecodeError as e:
            logger.error("yt-dlp JSON parse error: %s", e)
            return None, f"JSON parse error: {e}"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "yt-dlp info error (attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                e,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    return None, f"Failed after {MAX_RETRIES} attempts"


async def extract_pornhub_qualities(url: str) -> Tuple[List[dict], str]:
    """
    استخراج کیفیت‌های واقعی موجود از PornHub.

    Returns:
        (qualities, title)
    """
    if not is_pornhub_url(url):
        logger.warning("Not a valid PornHub URL: %s", url)
        return [], "Invalid URL"

    cleanup_expired_sessions()

    data, error = await _run_ytdlp_json(url)
    if data is None:
        return [], error or "Failed to get video info"

    title = data.get("title", "Untitled")
    duration = data.get("duration")
    duration_str = ""
    if duration:
        mins, secs = divmod(int(duration), 60)
        duration_str = f"{mins}:{secs:02d}"

    formats = data.get("formats", [])
    if not formats:
        return [], "No formats available"

    has_ffmpeg = _check_ffmpeg()
    qualities: List[dict] = []
    seen_heights = set()

    for fmt in formats:
        format_id = fmt.get("format_id", "")
        protocol = fmt.get("protocol", "")
        height = fmt.get("height")
        resolution = fmt.get("resolution", "")
        filesize = fmt.get("filesize") or fmt.get("filesize_approx")

        # بدون ffmpeg فقط HTTPS formats
        is_hls = protocol in ("m3u8", "m3u8_native")
        if is_hls and not has_ffmpeg:
            logger.debug("Skipping HLS format %s (no ffmpeg)", format_id)
            continue

        # استخراج height از resolution اگه نبود
        if not height and resolution:
            h_match = re.search(r"(\d+)p", resolution)
            if h_match:
                height = int(h_match.group(1))

        if not height:
            continue

        # جلوگیری از تکرار همون height
        # ترجیح HTTPS بر HLS
        height_key = height
        if height_key in seen_heights and is_hls:
            continue
        seen_heights.add(height_key)

        # ساخت label
        label = f"📺 {height}p"
        if is_hls:
            label += " (HLS)"
        else:
            label += " (MP4)"

        if filesize:
            label += f" ~{_format_size(int(filesize))}"

        if duration_str:
            label += f" [{duration_str}]"

        qualities.append(
            {
                "label": label,
                "url": url,
                "format_id": format_id,
                "method": "ytdlp",
                "height": height,
                "protocol": protocol,
                "filesize": filesize,
            }
        )

    # مرتب‌سازی: بالاترین کیفیت اول
    qualities.sort(key=lambda q: q.get("height", 0), reverse=True)

    logger.info(
        "Extracted %d qualities for: %s (ffmpeg=%s)",
        len(qualities),
        title[:60],
        has_ffmpeg,
    )
    return qualities, title


# ─── Download: Multi-segment (fast) ────────────────────────

import aiohttp
import aiofiles
from aiohttp import ClientTimeout


async def _download_multi_segment(
    direct_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback],
    num_segments: int = 16,
) -> Tuple[bool, str, int]:
    """
    دانلود چند تیکه‌ای با چند connection همزمان.
    هر تیکه یه Range request جدا میزنه → سرعت N برابر.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.pornhub.com/",
    }

    try:
        timeout = ClientTimeout(total=30, connect=15)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.head(direct_url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HEAD failed: HTTP {resp.status}", 0

                content_length = int(resp.headers.get("Content-Length", 0))
                accept_ranges = resp.headers.get("Accept-Ranges", "")

                if content_length == 0:
                    return False, "Cannot determine file size", 0
                if content_length > MAX_DOWNLOAD_SIZE:
                    return False, f"File too large: {_format_size(content_length)}", 0
                if accept_ranges.lower() != "bytes":
                    return False, "Range not supported", 0

        total_mb = content_length / 1024 / 1024
        if progress_cb:
            await progress_cb(
                f"📥 **دانلود سریع ({num_segments} بخش)...**\n💾 حجم: {total_mb:.1f} MB"
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
                                    1024 * 1024
                                ):
                                    if not chunk:
                                        continue
                                    await f.write(chunk)
                                    downloaded_bytes[seg_idx] += len(chunk)

                                    now = time.time()
                                    async with lock:
                                        if now - last_update[0] >= 2.0 and progress_cb:
                                            last_update[0] = now
                                            total_dl = sum(downloaded_bytes)
                                            elapsed = now - start_time
                                            speed = (
                                                total_dl / elapsed if elapsed > 0 else 0
                                            )
                                            dl_mb = total_dl / 1024 / 1024
                                            pct = total_dl / content_length * 100
                                            filled = int(pct / 5)
                                            bar = "█" * filled + "░" * (20 - filled)
                                            speed_mb = min(speed / 1024 / 1024, 999)
                                            eta_secs = (
                                                int((content_length - total_dl) / speed)
                                                if speed > 0
                                                else 0
                                            )
                                            eta_m, eta_s = divmod(eta_secs, 60)

                                            await progress_cb(
                                                f"📥 **Downloading...**\n"
                                                f"`[{bar}]`\n"
                                                f"💾 {dl_mb:.1f}/{total_mb:.1f} MB  •  "
                                                f"⚡ {speed_mb:.1f} MB/s\n"
                                                f"📊 {pct:.1f}%  •  "
                                                f"⏱ ETA: {eta_m}:{eta_s:02d}"
                                            )
                    return
                except asyncio.CancelledError:
                    _cleanup_file(seg_file)
                    raise
                except Exception as e:
                    logger.warning(
                        "Segment %d attempt %d failed: %s", seg_idx, attempt + 1, e
                    )
                    _cleanup_file(seg_file)
                    downloaded_bytes[seg_idx] = 0
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

        if progress_cb:
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

        final_size = os.path.getsize(filepath)
        if final_size == 0:
            _cleanup_file(filepath)
            return False, "Merged file is empty", 0

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


# ─── Get direct URL from yt-dlp ────────────────────────────


async def _get_direct_url(url: str, format_id: str) -> Tuple[Optional[str], str]:
    """
    گرفتن لینک مستقیم دانلود از yt-dlp بدون دانلود.

    Returns:
        (direct_url, error)
    """
    if format_id in ("best", ""):
        format_selector = "best"
    else:
        format_selector = f"{format_id}/best"

    cmd = _ytdlp_base_cmd() + [
        "--format",
        format_selector,
        "--get-url",
        url,
    ]

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=YTDLP_INFO_TIMEOUT,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
                continue

            if process.returncode == 0:
                direct_url = stdout.decode(errors="replace").strip().splitlines()[0]
                if direct_url.startswith("http"):
                    return direct_url, ""
                return None, "Invalid URL from yt-dlp"

            err = _extract_ytdlp_error(stderr.decode(errors="replace"))
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)
                continue
            return None, err

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)
            else:
                return None, str(e)[:200]

    return None, f"Failed after {MAX_RETRIES} attempts"


# ─── Main download function (updated) ──────────────────────


async def download_pornhub_video(
    url: str,
    format_id: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو PornHub.

    استراتژی:
      1. لینک مستقیم رو با yt-dlp --get-url بگیر
      2. اول multi-segment (16 connection) امتحان کن
      3. اگه نشد، fallback به yt-dlp عادی
    """
    if not is_pornhub_url(url):
        return False, "URL host not allowed", 0

    if not _check_ytdlp():
        return False, "yt-dlp is not installed", 0

    # ── مرحله 1: گرفتن لینک مستقیم ──
    is_hls = format_id.startswith("hls")

    if not is_hls:
        if progress_cb:
            await progress_cb("🔍 **دریافت لینک دانلود...**")

        direct_url, err = await _get_direct_url(url, format_id)

        if direct_url:
            logger.info("Trying multi-segment download (16 connections)")
            success, error, size = await _download_multi_segment(
                direct_url,
                filepath,
                progress_cb,
                num_segments=16,
            )
            if success:
                return True, "", size

            logger.info("Multi-segment failed: %s, falling back to yt-dlp", error)
            _cleanup_file(filepath)

    # ── مرحله 3: Fallback به yt-dlp عادی ──
    logger.info("Falling back to yt-dlp download")
    return await _download_with_ytdlp(url, format_id, filepath, progress_cb)


async def _download_with_ytdlp(
    url: str,
    format_id: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback],
) -> Tuple[bool, str, int]:
    """دانلود با yt-dlp (fallback)."""
    if format_id in ("best", ""):
        format_selector = "best"
    else:
        format_selector = f"{format_id}/best"

    cmd = _ytdlp_base_cmd() + [
        "--format",
        format_selector,
        "--output",
        filepath,
        "--max-filesize",
        str(MAX_DOWNLOAD_SIZE),
        "--retries",
        str(MAX_RETRIES),
        "--fragment-retries",
        str(MAX_RETRIES),
        "--progress",
        "--newline",
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
                        process.stdout.readline(),
                        timeout=120,
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
                        msg = _parse_progress_line(text)
                        if msg:
                            await progress_cb(msg)

            stderr_text = ""
            try:
                stderr_data = await asyncio.wait_for(
                    process.stderr.read(),
                    timeout=10,
                )
                stderr_text = stderr_data.decode(errors="replace").strip()
            except asyncio.TimeoutError:
                pass

            await process.wait()

            if process.returncode == 0:
                actual_path = _find_output_file(filepath)
                if actual_path is None:
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_DELAY * attempt)
                        continue
                    return False, "Output file not found", 0

                size = os.path.getsize(actual_path)
                if size < 1024:
                    _cleanup_file(actual_path)
                    return False, f"File too small ({size} bytes)", 0

                if actual_path != filepath:
                    try:
                        os.rename(actual_path, filepath)
                    except OSError:
                        pass

                logger.info("yt-dlp download complete: %s", _format_size(size))
                return True, "", size

            error_msg = _extract_ytdlp_error(stderr_text)
            logger.warning(
                "yt-dlp failed (attempt %d/%d, code %d): %s",
                attempt,
                MAX_RETRIES,
                process.returncode,
                error_msg,
            )

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
    return False, f"Failed after {MAX_RETRIES} attempts: {error_msg}", 0


def _parse_progress_line(text: str) -> Optional[str]:
    """پارس خط progress yt-dlp و ساخت پیام زیبا."""
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


def _extract_ytdlp_error(stderr: str) -> str:
    """استخراج پیام خطای اصلی از stderr yt-dlp."""
    if not stderr:
        return "Unknown error"

    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("ERROR:"):
            return line[6:].strip()[:200]

    # fallback: آخرین خط غیرخالی
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    if lines:
        return lines[-1][:200]

    return "Unknown error"


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


# ─── Convenience wrappers (سازگاری با bot) ─────────────────


async def download_pornhub_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    format_id: str = "best",
) -> Tuple[bool, str, int]:
    """Wrapper برای دانلود."""
    return await download_pornhub_video(url, format_id, filepath, progress_cb)


async def download_pornhub_m3u8(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    format_id: str = "best",
) -> Tuple[bool, str, int]:
    """Wrapper برای دانلود HLS."""
    return await download_pornhub_video(url, format_id, filepath, progress_cb)
