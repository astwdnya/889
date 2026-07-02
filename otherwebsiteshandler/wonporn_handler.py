"""
wonporn_handler.py
──────────────────
Extract and download videos from wonporn.com

Strategy:
  - Uses pure aiohttp (No Playwright needed -> Extremely lightweight)
  - Maintains a persistent CookieJar to bypass Time-limited Tokens
  - Cleans malformed URLs from HTML before downloading
  - Validates file size to prevent saving error HTML pages

Dependencies:
    pip install aiohttp aiofiles
"""

import asyncio
import logging
import os
import re
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse, unquote

import aiofiles
import aiohttp
from aiohttp import ClientTimeout, CookieJar

logger = logging.getLogger("WonPornHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MAX_DOWNLOAD_SIZE = 4 * 1024 * 1024 * 1024  # 4 GB
MIN_VALID_VIDEO_SIZE = 100 * 1024  # 100 KB (If smaller, it's an error page)
CHUNK_SIZE = 1024 * 1024  # 1 MB
PROGRESS_INTERVAL = 2.0
MAX_RETRIES = 3
RETRY_DELAY = 2.0

_ALLOWED_HOSTS = frozenset({
    "wonporn.com",
    "www.wonporn.com",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_wonporn_url(url: str) -> bool:
    """Check if the URL belongs to wonporn.com."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS
    except Exception:
        return False


def _cleanup_file(filepath: str) -> None:
    """Delete file if it exists."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Cleanup failed %s: %s", filepath, e)


def _format_size(size_bytes: int) -> str:
    """Format bytes into human-readable size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def _clean_url(url: str) -> str:
    """
    Clean malformed URLs extracted from HTML.
    Removes trailing slashes and HTML entities like &amp;
    """
    url = unquote(url).replace("&amp;", "&")
    url = re.sub(r'[\\/]+$', '', url)
    return url


# ─── Extraction ─────────────────────────────────────────────


def _extract_title(html: str) -> str:
    """Extract video title from HTML."""
    # <title>
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*wonporn\.com.*$", "", title, flags=re.I).strip()
        if title:
            return title
    return "Untitled"


def _extract_video_url(html: str) -> Optional[str]:
    """
    Extract the clean MP4 URL from HTML.
    WonPorn uses a standard HTML5 <video> tag.
    """
    # Priority 1: Find URL strictly inside standard <video> tag
    vid_match = re.search(
        r'<video[^>]*src=["\'](https?://[^"\']+\.mp4[^"\']*)["\']',
        html, re.I
    )
    if vid_match:
        return _clean_url(vid_match.group(1))
    
    # Priority 2: Fallback to generic MP4 search in source
    all_mp4s = re.findall(r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)', html, re.I)
    if all_mp4s:
        return _clean_url(all_mp4s[0])
        
    return None


# ─── Main API ──────────────────────────────────────────────


async def extract_wonporn_qualities(
    url: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict]:
    """
    Extract video qualities.
    Note: WonPorn serves a single MP4 quality.

    Returns:
        (qualities, title, info)
    """
    if not is_wonporn_url(url):
        return [], "Invalid URL", {}

    if progress_cb:
        await progress_cb("🔄 **Fetching page info...**")

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    timeout = ClientTimeout(total=30, connect=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    return [], f"HTTP {resp.status}", {}
                
                html = await resp.text(errors="replace")
    except Exception as e:
        logger.error("Failed to fetch page: %s", e)
        return [], str(e), {}

    video_url = _extract_video_url(html)
    title = _extract_title(html)

    if not video_url:
        return [], "Video URL not found in page", {}

    qualities = [
        {
            "label": "📺 MP4",
            "url": video_url,
            "height": 720,  # WonPorn generally serves 720p/1080p consolidated
            "quality_key": "default",
        }
    ]

    logger.info("Found quality for: %s", title[:60])
    return qualities, title, {}


# ─── Download ───────────────────────────────────────────────


async def download_wonporn_video(
    page_url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[bool, str, int]:
    """
    Download video using a persistent session to keep cookies alive.
    This is REQUIRED because WonPorn uses Time-limited Tokens.

    Returns:
        (success, error_message, file_size)
    """
    if not is_wonporn_url(page_url):
        return False, "URL not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0

    # A persistent CookieJar is crucial to pass the token validation
    jar = CookieJar(unsafe=True)
    base_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with aiohttp.ClientSession(
                timeout=timeout, headers=base_headers, cookie_jar=jar
            ) as session:
                
                # Step 1: Fetch the page to generate and capture session cookies
                if progress_cb:
                    await progress_cb("🔄 **Generating secure token...**")
                    
                async with session.get(page_url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        error = f"Page HTTP {resp.status}"
                        if 400 <= resp.status < 500:
                            return False, error, 0
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(RETRY_DELAY * attempt)
                            continue
                        return False, error, 0
                    
                    html = await resp.text(errors="replace")
                    # Re-extract URL inside the session to ensure it matches the new cookies
                    fresh_url = _extract_video_url(html)
                    if fresh_url:
                        video_url = fresh_url

                # Step 2: Download the video using the exact same session
                download_headers = {
                    "Referer": page_url,
                    "Accept": "*/*",
                }

                if progress_cb:
                    await progress_cb("📥 **Starting download...**")

                async with session.get(
                    video_url, headers=download_headers, allow_redirects=True
                ) as resp:
                    if resp.status != 200:
                        error = f"Video HTTP {resp.status}"
                        if 400 <= resp.status < 500:
                            return False, error, 0
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(RETRY_DELAY * attempt)
                            continue
                        return False, error, 0

                    content_length = int(
                        resp.headers.get("Content-Length", 0)
                    )
                    
                    # Safety check: WonPorn returns error HTML if token fails.
                    # Valid videos are always > 100KB.
                    if 0 < content_length < MIN_VALID_VIDEO_SIZE:
                        _cleanup_file(filepath)
                        return False, "Server returned error page (Token expired/Invalid)", 0

                    if content_length > MAX_DOWNLOAD_SIZE:
                        return (
                            False,
                            f"Too large: {_format_size(content_length)}",
                            0,
                        )

                    total_mb = (
                        content_length / 1024 / 1024
                        if content_length
                        else 0
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
                                return False, "Exceeded size limit", 0

                            now = time.time()
                            if (
                                progress_cb
                                and now - last_update >= PROGRESS_INTERVAL
                            ):
                                last_update = now
                                await _report_progress(
                                    progress_cb,
                                    downloaded,
                                    content_length,
                                    total_mb,
                                    start_time,
                                )

            size = os.path.getsize(filepath)
            if size < MIN_VALID_VIDEO_SIZE:
                _cleanup_file(filepath)
                return False, f"Downloaded file is too small ({size} bytes)", 0

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
    """Generate and send progress message."""
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


# ─── Wrapper ────────────────────────────────────────────────


async def download_wonporn_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    """Wrapper compatible with bot architecture."""
    return await download_wonporn_video(
        url, video_url, filepath, progress_cb
    )