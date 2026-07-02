"""
heavyr_handler.py
─────────────────
Extract and download videos from heavy-r.com (FluidPlayer)

Strategy:
  - Bypass Cloudflare using Playwright (Browser closes immediately after extraction)
  - Extract direct MP4 URL from HTML <video> tag
  - Fast & lightweight download using aiohttp (No Base64, No browser download)

Dependencies:
    pip install playwright aiohttp aiofiles
    playwright install chromium
"""

import asyncio
import logging
import os
import re
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("HeavyRHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_ANTI_BOT_JS = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = {runtime: {}};
"""

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled"
]

MAX_DOWNLOAD_SIZE = 4 * 1024 * 1024 * 1024  # 4 GB
MIN_FILE_SIZE = 1024
CHUNK_SIZE = 1024 * 1024  # 1 MB
PROGRESS_INTERVAL = 2.0
MAX_RETRIES = 3
RETRY_DELAY = 2.0
CF_WAIT_TIMEOUT = 30  # seconds

_ALLOWED_HOSTS = frozenset({
    "heavy-r.com",
    "www.heavy-r.com",
})

# Domains used for ads/pre-roll videos that must be filtered out
_AD_DOMAINS = frozenset({
    "trudigo.com",
    "bngdin.com",
    "awdeliverynet.com",
})

# The official CDN host for the actual video file
_OFFICIAL_CDN = "b-cdn.heavy-r.com"

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_heavyr_url(url: str) -> bool:
    """Check if the URL belongs to heavy-r.com."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS
    except Exception:
        return False


def _is_ad_url(url: str) -> bool:
    """Check if the URL belongs to an advertising network."""
    try:
        host = urlparse(url).hostname or ""
        return any(ad in host for ad in _AD_DOMAINS)
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


# ─── Playwright Browser Manager ────────────────────────────


async def _create_browser_context(playwright):
    """Create browser context with anti-detection settings."""
    browser = await playwright.chromium.launch(
        headless=True,
        args=_LAUNCH_ARGS,
    )
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    await context.add_init_script(_ANTI_BOT_JS)
    return browser, context


async def _wait_for_cloudflare(page, timeout: int = CF_WAIT_TIMEOUT) -> bool:
    """Wait until Cloudflare challenge is solved."""
    for _ in range(timeout // 2):
        await asyncio.sleep(2)
        try:
            html = await page.content()
            if "Just a moment" not in html and len(html) > 5000:
                return True
        except Exception:
            continue
    return False


# ─── Extraction ─────────────────────────────────────────────


def _extract_title(html: str) -> str:
    """Extract video title from HTML."""
    # og:title
    m = re.search(
        r'property=["\']og:title["\']\s+content=["\']([^"\']+)',
        html, re.I,
    )
    if m:
        return m.group(1).strip()

    # <title>
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[|]\s*Heavy-R\.com.*$", "", title, flags=re.I).strip()
        if title:
            return title

    return "Untitled"


def _extract_thumbnail(html: str) -> str:
    """Extract video thumbnail from HTML."""
    m = re.search(
        r'property=["\']og:image["\']\s+content=["\']([^"\']+)',
        html, re.I,
    )
    if m:
        return m.group(1).strip()

    m = re.search(r'poster=["\']([^"\']+)', html, re.I)
    return m.group(1).strip() if m else ""


def _extract_qualities(html: str) -> List[dict]:
    """
    Extract video qualities.
    Heavy-R uses FluidPlayer, links are inside <video> or JS variables.
    """
    qualities = []
    seen_urls = set()

    # Priority 1: Look specifically for the official CDN to avoid ads
    cdn_pattern = rf'(https?://{_OFFICIAL_CDN.replace(".", r"\.")}/[^\s"\'<>]+\.mp4[^\s"\'<>]*)'
    for m in re.finditer(cdn_pattern, html, re.I):
        url = m.group(1).strip()
        if url not in seen_urls:
            seen_urls.add(url)
            qualities.append({
                "label": "📺 MP4",
                "url": url,
                "height": 720,  # Heavy-R usually serves a single consolidated quality
                "quality_key": "default",
            })

    # Priority 2: Fallback to generic MP4 search if official CDN failed
    if not qualities:
        generic_pattern = r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)'
        for m in re.finditer(generic_pattern, html, re.I):
            url = m.group(1).strip()
            if url not in seen_urls and not _is_ad_url(url):
                seen_urls.add(url)
                qualities.append({
                    "label": "📺 MP4",
                    "url": url,
                    "height": 720,
                    "quality_key": "default",
                })

    return qualities


# ─── Main API ──────────────────────────────────────────────


async def extract_heavyr_qualities(
    url: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict]:
    """
    Extract video qualities.

    Returns:
        (qualities, title, info)
    """
    if not is_heavyr_url(url):
        return [], "Invalid URL", {}

    from playwright.async_api import async_playwright

    if progress_cb:
        await progress_cb("🌐 **Opening page...**")

    try:
        async with async_playwright() as p:
            browser, context = await _create_browser_context(p)
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning("Page goto exception: %s", e)

            if progress_cb:
                await progress_cb("🔄 **Bypassing protection...**")

            solved = await _wait_for_cloudflare(page)
            if not solved:
                await browser.close()
                return [], "Cloudflare blocked", {}

            html = await page.content()
            await browser.close()  # Free up RAM immediately

    except Exception as e:
        logger.error("Playwright error: %s", e)
        return [], str(e), {}

    qualities = _extract_qualities(html)
    title = _extract_title(html)
    thumbnail = _extract_thumbnail(html)

    info = {"thumbnail": thumbnail}

    if qualities:
        logger.info("Found %d qualities for: %s", len(qualities), title[:60])
    else:
        logger.warning("No qualities found for: %s", url)

    return qualities, title, info


# ─── Download ───────────────────────────────────────────────


async def download_heavyr_video(
    page_url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[bool, str, int]:
    """
    Download video using aiohttp.

    Returns:
        (success, error_message, file_size)
    """
    if not is_heavyr_url(page_url):
        return False, "URL not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0

    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": page_url,
        "Accept": "*/*",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)

            async with aiohttp.ClientSession(
                timeout=timeout
            ) as session:
                async with session.get(
                    video_url,
                    headers=headers,
                    allow_redirects=True,
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

                    if progress_cb:
                        size_str = (
                            _format_size(content_length)
                            if content_length
                            else "unknown"
                        )
                        await progress_cb(
                            f"📥 **Starting download...**\n💾 Size: {size_str}"
                        )

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


async def download_heavyr_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    """Wrapper compatible with bot architecture."""
    return await download_heavyr_video(
        url, video_url, filepath, progress_cb
    )