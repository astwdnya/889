"""
youporn_handler.py
------------------
استخراج لینک‌های دانلود از youporn.com با استفاده از yt-dlp.

روش کار:
  - YouPorn extractor رسمی در yt-dlp داره (YouPornIE)
  - yt-dlp --dump-json لیست formats رو می‌ده (direct mp4 + HLS)
  - برای هر کیفیت direct mp4 ترجیح داده میشه، HLS fallback
  - دانلود هم با yt-dlp انجام میشه
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("YouPornHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

_SITE_DOMAIN = "youporn.com"
_SITE_URL = "https://www.youporn.com"
_SITE_REFERER = f"{_SITE_URL}/"

_ALLOWED_HOSTS = frozenset(
    {
        "youporn.com",
        "www.youporn.com",
    }
)

_ALLOWED_HOST_SUFFIXES = (
    ".youporn.com",
    ".ypncdn.com",
)

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_youporn_url(url: str) -> bool:
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


def _check_impersonation_support() -> bool:
    try:
        import curl_cffi  # noqa: F401

        return True
    except ImportError:
        return False


def _find_output_file(filepath: str) -> Optional[str]:
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


def _quality_sort_key(q: dict) -> int:
    nums = re.findall(r"\d+", q["label"])
    return int(nums[-1]) if nums else 0


# ─── yt-dlp extraction ──────────────────────────────────────


async def _ytdlp_dump(url: str) -> Optional[dict]:
    """اجرای yt-dlp --dump-json و برگرداندن dict."""
    if not shutil.which("yt-dlp"):
        return None

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-download",
        "--dump-json",
        "--no-check-certificates",
        "--no-playlist",
    ]
    if _check_impersonation_support():
        cmd.extend(["--impersonate", "chrome"])
    cmd.append(url)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("yt-dlp timed out for %s", url[:60])
            return None

        if process.returncode != 0:
            logger.debug("yt-dlp failed: %s", stderr.decode(errors="replace")[:200])
            return None

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return None
        return json.loads(raw.split("\n")[0])
    except json.JSONDecodeError:
        return None
    except Exception as e:
        logger.warning("yt-dlp dump error: %s", e)
        return None


def _parse_formats(data: dict) -> List[dict]:
    """
    تبدیل formats به لیست کیفیت.
    برای هر ارتفاع، direct mp4 ترجیح داده میشه و HLS به عنوان جایگزین.
    """
    formats = data.get("formats", [])
    # گروه‌بندی بر اساس ارتفاع
    by_height: dict = {}

    for fmt in formats:
        if fmt.get("vcodec") == "none":
            continue
        height = fmt.get("height")
        url = fmt.get("url", "")
        if not height or not url:
            continue
        proto = fmt.get("protocol", "")
        is_m3u8 = proto in ("m3u8", "m3u8_native") or ".m3u8" in url

        entry = {
            "url": url,
            "format_id": fmt.get("format_id", ""),
            "is_m3u8": is_m3u8,
            "tbr": fmt.get("tbr") or 0,
        }
        by_height.setdefault(height, []).append(entry)

    qualities: List[dict] = []
    for height, entries in by_height.items():
        # direct mp4 ترجیح داده میشه (is_m3u8=False اول)
        entries.sort(key=lambda e: (e["is_m3u8"], -e["tbr"]))
        best = entries[0]
        qualities.append(
            {
                "label": f"🎥 {height}p",
                "url": best["url"],
                "method": "m3u8" if best["is_m3u8"] else "direct",
                "format_id": best["format_id"],
            }
        )

    qualities.sort(key=_quality_sort_key, reverse=True)
    return qualities


async def extract_youporn_qualities(url: str) -> Tuple[List[dict], str]:
    """لینک‌های کیفیت مختلف رو از YouPorn استخراج میکنه."""
    if not is_youporn_url(url):
        return [], "Invalid URL"

    if not shutil.which("yt-dlp"):
        return [], "yt-dlp نصب نیست. نصب: pip install yt-dlp"

    logger.info("Extracting YouPorn: %s", url)
    data = await _ytdlp_dump(url)
    if not data:
        return [], "Could not extract video (yt-dlp failed)"

    title = data.get("title", "Untitled")
    qualities = _parse_formats(data)

    if qualities:
        logger.info("Extracted %d qualities for: %s", len(qualities), title[:60])
        return qualities, title

    # fallback: لینک مستقیم تکی
    direct = data.get("url")
    if direct:
        return [
            {
                "label": "🎥 Video",
                "url": direct,
                "method": "m3u8" if ".m3u8" in direct else "direct",
                "format_id": data.get("format_id", ""),
            }
        ], title

    return [], "no video formats found"


# ─── Download (yt-dlp) ──────────────────────────────────────


async def _download_with_ytdlp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    format_id: Optional[str] = None,
) -> Tuple[bool, str, int]:
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
            "--concurrent-fragments",
            "16",
            "--retries",
            "10",
            "--fragment-retries",
            "10",
            "--retry-sleep",
            "fragment:exp=1:30",
            "--buffer-size",
            "16K",
            "--max-filesize",
            str(MAX_DOWNLOAD_SIZE),
            "--add-header",
            f"Referer:{_SITE_REFERER}",
            "--add-header",
            f"User-Agent:{_USER_AGENT}",
            "--merge-output-format",
            "mp4",
            "-o",
            filepath,
        ]

        if has_aria2c:
            cmd.extend(
                [
                    "--downloader",
                    "aria2c",
                    "--downloader-args",
                    "aria2c:-x16 -s16 -k1M --max-connection-per-server=16 "
                    "--min-split-size=1M --console-log-level=warn",
                ]
            )

        if _check_impersonation_support():
            cmd.extend(["--impersonate", "chrome"])
        if format_id:
            cmd.extend(["-f", format_id])
        else:
            cmd.extend(["-f", "best"])
        cmd.append(url)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        last_update = 0.0
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=180)
            except asyncio.TimeoutError:
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
                await progress_cb(f"📥 **Downloading...**\n`{text[:80]}`")

        await process.wait()
        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            return False, stderr[:200], 0

        actual_path = _find_output_file(filepath)
        if not actual_path:
            return False, "Output file not found", 0

        size = os.path.getsize(actual_path)
        if size == 0:
            _cleanup_file(actual_path)
            return False, "Downloaded file is empty", 0
        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(actual_path)
            return False, "File exceeds size limit", 0
        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        return False, str(e)[:150], 0


# ─── Download: Public API ──────────────────────────────────


async def download_youporn_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    format_id: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """دانلود ویدیوی YouPorn (direct mp4 یا HLS) با yt-dlp."""
    if not _is_allowed_host(url):
        return False, "URL host not allowed", 0

    success, error, size = await _download_with_ytdlp(
        url,
        filepath,
        progress_cb,
        format_id=format_id,
    )
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error, 0


async def download_youporn_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    format_id: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """دانلود HLS stream از YouPorn با yt-dlp."""
    if not _is_allowed_host(m3u8_url):
        return False, "URL host not allowed", 0

    success, error, size = await _download_with_ytdlp(
        m3u8_url,
        filepath,
        progress_cb,
        format_id=format_id,
    )
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error, 0
