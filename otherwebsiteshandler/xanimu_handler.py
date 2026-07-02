"""
xanimu_handler.py (Final - Playwright Edition)
───────────────────────────────────────────────
دانلود ویدیو از xanimu.com با bypass Cloudflare

پیش‌نیاز:
    pip install playwright aiofiles aiohttp
    playwright install chromium
"""

import asyncio
import base64
import html as html_lib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

import aiofiles

logger = logging.getLogger("XanimuHandler")

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

_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MIN_FILE_SIZE = 1024  # 1 KB
CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB per range request
CF_WAIT_TIMEOUT = 45  # ثانیه صبر برای Cloudflare
CF_CHECK_INTERVAL = 2

_ALLOWED_HOSTS = frozenset({"xanimu.com", "www.xanimu.com"})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_xanimu_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS
    except Exception:
        return False


def _cleanup_file(filepath: str) -> None:
    try:
        os.remove(filepath)
    except FileNotFoundError:
        pass
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


# ─── Playwright Browser Manager ────────────────────────────


async def _create_browser_context(playwright):
    """ساخت browser و context با تنظیمات anti-detection."""
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
    """صبر تا Cloudflare challenge حل بشه."""
    for i in range(timeout // CF_CHECK_INTERVAL):
        await asyncio.sleep(CF_CHECK_INTERVAL)
        try:
            html = await page.content()
            if "Just a moment" not in html and len(html) > 3000:
                return True
        except Exception:
            continue
    return False


# ─── Extraction ─────────────────────────────────────────────


def _extract_title(html: str) -> str:
    """استخراج عنوان ویدیو."""
    # JSON title
    m = re.search(r'"title"\s*:\s*"([^"]+)"', html)
    if m:
        title = m.group(1).strip()
        if len(title) > 3:
            return html_lib.unescape(title)

    # <title>
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*XAnimu\.com\s*$", "", title, flags=re.I).strip()
        if title:
            return html_lib.unescape(title)

    # og:title
    m = re.search(r'property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.I)
    if m:
        return html_lib.unescape(m.group(1).strip())

    return "Untitled"


def _extract_qualities(html: str) -> List[dict]:
    """استخراج کیفیت‌های ویدیو از HTML."""
    qualities = []
    seen = set()

    # source tags (اصلی‌ترین روش)
    for m in re.finditer(
        r"<source[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I
    ):
        url = html_lib.unescape(m.group(1).strip())
        if url in seen:
            continue
        seen.add(url)
        is_high = "_high" in url
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} Quality",
            "url": url,
            "height": 720 if is_high else 360,
            "quality_key": "high" if is_high else "low",
        })

    # video src
    m = re.search(r"<video[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I)
    if m:
        url = html_lib.unescape(m.group(1).strip())
        if url not in seen:
            seen.add(url)
            is_high = "_high" in url
            qualities.append({
                "label": f"📺 {'High' if is_high else 'Low'} Quality",
                "url": url,
                "height": 720 if is_high else 360,
                "quality_key": "high" if is_high else "low",
            })

    # JS vars
    for var_name, key, default_h in [("videoHigh", "high", 720), ("videoLow", "low", 360)]:
        vm = re.search(rf'var\s+{var_name}\s*=\s*"([^"]+)"', html)
        if vm:
            url = vm.group(1).strip()
            if url not in seen:
                seen.add(url)
                title_m = re.search(rf'var\s+{var_name}Title\s*=\s*"([^"]+)"', html)
                label = title_m.group(1) if title_m else key.title()
                qualities.append({
                    "label": f"📺 {label} Quality",
                    "url": url,
                    "height": default_h,
                    "quality_key": key,
                })

    # مرتب‌سازی: high اول
    qualities.sort(key=lambda q: q.get("height", 0), reverse=True)

    # حذف تکراری بر اساس quality_key
    unique = {}
    for q in qualities:
        k = q["quality_key"]
        if k not in unique:
            unique[k] = q
    return sorted(unique.values(), key=lambda q: q["height"], reverse=True)


def _extract_video_info(html: str) -> dict:
    """استخراج اطلاعات اضافی ویدیو."""
    info = {}

    # toStore (JSON نیست چون new Date() داره، regex بزن)
    for key, pattern in [
        ("views", r'"views"\s*:\s*(\d+)'),
        ("post_id", r'"postId"\s*:\s*(\d+)'),
        ("rate_count", r'"rateCount"\s*:\s*(\d+)'),
    ]:
        m = re.search(pattern, html)
        if m:
            info[key] = int(m.group(1))

    for key, pattern in [
        ("duration", r'"length"\s*:\s*"([^"]+)"'),
        ("likes", r'"likes"\s*:\s*"([^"]+)"'),
        ("title", r'"title"\s*:\s*"([^"]+)"'),
    ]:
        m = re.search(pattern, html)
        if m:
            info[key] = m.group(1)

    # thumbnail
    m = re.search(r"poster=[\"']([^\"']+)[\"']", html)
    if m:
        info["thumbnail"] = m.group(1)

    return info


# ─── Main API ──────────────────────────────────────────────


async def extract_xanimu_qualities(
    url: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict]:
    """
    استخراج کیفیت‌های ویدیو.

    Returns:
        (qualities, title, info)
    """
    if not is_xanimu_url(url):
        return [], "Invalid URL", {}

    from playwright.async_api import async_playwright

    if progress_cb:
        await progress_cb("🌐 **در حال باز کردن صفحه...**")

    try:
        async with async_playwright() as p:
            browser, context = await _create_browser_context(p)
            page = await context.new_page()

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning("Page goto: %s", e)

            if progress_cb:
                await progress_cb("🔄 **در حال بارگذاری صفحه...**")

            solved = await _wait_for_cloudflare(page)
            if not solved:
                await browser.close()
                return [], "Cloudflare blocked", {}

            html = await page.content()
            await browser.close()

    except Exception as e:
        logger.error("Playwright error: %s", e)
        return [], str(e), {}

    qualities = _extract_qualities(html)
    title = _extract_title(html)
    info = _extract_video_info(html)

    if qualities:
        logger.info("Found %d qualities for: %s", len(qualities), title[:60])
    else:
        logger.warning("No qualities found for: %s", url)

    return qualities, title, info


async def download_xanimu_video(
    page_url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو از xanimu.

    فلو:
        1. Playwright میره CDN URL → Cloudflare حل میشه
        2. با JS fetch + Range header فایل chunk chunk دانلود میشه

    Returns:
        (success, error_message, file_size)
    """
    if not is_xanimu_url(page_url):
        return False, "URL not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0

    from playwright.async_api import async_playwright

    if progress_cb:
        await progress_cb("🌐 **آماده‌سازی دانلود...**")

    try:
        async with async_playwright() as p:
            browser, context = await _create_browser_context(p)
            page = await context.new_page()

            # ─── فاز 1: حل Cloudflare CDN ───
            if progress_cb:
                await progress_cb("🔄 **حل Cloudflare CDN...**\n⏳ ممکنه تا 30 ثانیه طول بکشه")

            cf_solved = asyncio.Event()

            async def on_video_response(response):
                ct = response.headers.get("content-type", "")
                if "video" in ct:
                    cf_solved.set()

            page.on("response", on_video_response)

            try:
                await page.goto(video_url, wait_until="commit", timeout=60000)
            except Exception:
                pass

            try:
                await asyncio.wait_for(cf_solved.wait(), timeout=CF_WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("CDN CF timeout, continuing anyway")

            await asyncio.sleep(2)

            # ─── فاز 2: page جدید برای دانلود ───
            await page.close()
            dl_page = await context.new_page()

            cdn_host = urlparse(video_url).hostname
            try:
                await dl_page.goto(
                    f"https://{cdn_host}/", wait_until="commit", timeout=15000
                )
            except Exception:
                pass
            await asyncio.sleep(1)

            # ─── فاز 3: سایز فایل ───
            file_info = await dl_page.evaluate("""
                async (url) => {
                    try {
                        const resp = await fetch(url, {
                            method: 'HEAD',
                            credentials: 'include',
                        });
                        return {
                            status: resp.status,
                            contentType: resp.headers.get('content-type'),
                            contentLength: parseInt(
                                resp.headers.get('content-length') || '0'
                            ),
                            acceptRanges: resp.headers.get('accept-ranges'),
                            ok: resp.ok,
                        };
                    } catch(e) {
                        return {error: e.message};
                    }
                }
            """, video_url)

            if not file_info.get("ok"):
                # fallback: Range probe
                file_info = await dl_page.evaluate("""
                    async (url) => {
                        try {
                            const resp = await fetch(url, {
                                credentials: 'include',
                                headers: {'Range': 'bytes=0-0'},
                            });
                            const cr = resp.headers.get('content-range');
                            let total = 0;
                            if (cr) {
                                const m = cr.match(/\\/(\\d+)/);
                                if (m) total = parseInt(m[1]);
                            }
                            return {
                                status: resp.status,
                                contentLength: total,
                                acceptRanges: 'bytes',
                                ok: true,
                            };
                        } catch(e) {
                            return {error: e.message};
                        }
                    }
                """, video_url)

            if file_info.get("error"):
                await browser.close()
                return False, f"File info error: {file_info['error']}", 0

            total_size = file_info.get("contentLength", 0)

            if total_size > MAX_DOWNLOAD_SIZE:
                await browser.close()
                return False, f"Too large: {_format_size(total_size)}", 0

            total_mb = total_size / 1024 / 1024 if total_size else 0

            if progress_cb:
                await progress_cb(
                    f"📥 **شروع دانلود...**\n💾 حجم: {total_mb:.1f} MB"
                )

            # ─── فاز 4: دانلود با Range requests ───
            downloaded = 0
            start_time = time.time()
            last_progress = 0.0

            try:
                async with aiofiles.open(filepath, "wb") as f:
                    while downloaded < total_size or total_size == 0:
                        range_start = downloaded
                        range_end = downloaded + CHUNK_SIZE - 1
                        if total_size > 0:
                            range_end = min(range_end, total_size - 1)

                        chunk_result = await dl_page.evaluate("""
                            async ([url, start, end]) => {
                                try {
                                    const resp = await fetch(url, {
                                        credentials: 'include',
                                        headers: {
                                            'Range': `bytes=${start}-${end}`,
                                        },
                                    });
                                    if (resp.status !== 206 && resp.status !== 200) {
                                        return {error: `HTTP ${resp.status}`};
                                    }
                                    const buf = await resp.arrayBuffer();
                                    const bytes = new Uint8Array(buf);
                                    let bin = '';
                                    const C = 8192;
                                    for (let i = 0; i < bytes.length; i += C) {
                                        bin += String.fromCharCode.apply(
                                            null,
                                            bytes.subarray(i, Math.min(i+C, bytes.length))
                                        );
                                    }
                                    return {data: btoa(bin), size: buf.byteLength};
                                } catch(e) {
                                    return {error: e.message};
                                }
                            }
                        """, [video_url, range_start, range_end])

                        if chunk_result.get("error"):
                            # retry
                            await asyncio.sleep(2)
                            chunk_result = await dl_page.evaluate("""
                                async ([url, start, end]) => {
                                    try {
                                        const resp = await fetch(url, {
                                            credentials: 'include',
                                            headers: {'Range': `bytes=${start}-${end}`},
                                        });
                                        if (resp.status !== 206 && resp.status !== 200) {
                                            return {error: `HTTP ${resp.status}`};
                                        }
                                        const buf = await resp.arrayBuffer();
                                        const bytes = new Uint8Array(buf);
                                        let bin = '';
                                        const C = 8192;
                                        for (let i = 0; i < bytes.length; i += C) {
                                            bin += String.fromCharCode.apply(
                                                null,
                                                bytes.subarray(i, Math.min(i+C, bytes.length))
                                            );
                                        }
                                        return {data: btoa(bin), size: buf.byteLength};
                                    } catch(e) {
                                        return {error: e.message};
                                    }
                                }
                            """, [video_url, range_start, range_end])

                            if chunk_result.get("error"):
                                _cleanup_file(filepath)
                                await browser.close()
                                return (
                                    False,
                                    f"Chunk error: {chunk_result['error']}",
                                    0,
                                )

                        chunk_data = base64.b64decode(chunk_result["data"])
                        await f.write(chunk_data)
                        downloaded += len(chunk_data)

                        # progress
                        now = time.time()
                        if progress_cb and now - last_progress >= 2.0:
                            last_progress = now
                            elapsed = now - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            dl_mb = downloaded / 1024 / 1024
                            speed_kb = min(speed / 1024, 99999)

                            if total_size > 0:
                                pct = downloaded / total_size * 100
                                filled = int(pct / 5)
                                bar = "█" * filled + "░" * (20 - filled)
                                eta_secs = (
                                    int((total_size - downloaded) / speed)
                                    if speed > 0
                                    else 0
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
                                    f"💾 {dl_mb:.1f} MB  •  "
                                    f"⚡ {speed_kb:.0f} KB/s"
                                )

                        # چک تمام شدن
                        actual = chunk_result.get("size", 0)
                        if actual < CHUNK_SIZE and total_size == 0:
                            break
                        if total_size > 0 and downloaded >= total_size:
                            break

            except asyncio.CancelledError:
                _cleanup_file(filepath)
                await browser.close()
                raise
            except Exception as e:
                _cleanup_file(filepath)
                await browser.close()
                return False, str(e)[:200], 0

            await browser.close()

            # ─── چک فایل ───
            if not os.path.exists(filepath):
                return False, "File not created", 0

            size = os.path.getsize(filepath)

            if size < MIN_FILE_SIZE:
                _cleanup_file(filepath)
                return False, f"Too small ({size} bytes)", 0

            # چک HTML
            with open(filepath, "rb") as f:
                header = f.read(32)
            if b"<!DOCTYPE" in header or b"<html" in header:
                _cleanup_file(filepath)
                return False, "Downloaded HTML instead of video", 0

            logger.info("Download complete: %s", _format_size(size))
            return True, "", size

    except Exception as e:
        _cleanup_file(filepath)
        logger.error("Download error: %s", e)
        return False, str(e)[:200], 0


# ─── Wrapper ────────────────────────────────────────────────


async def download_xanimu_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    """Wrapper سازگار با bot."""
    return await download_xanimu_video(url, video_url, filepath, progress_cb)
