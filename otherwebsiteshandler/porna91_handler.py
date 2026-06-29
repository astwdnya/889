"""
porna91_handler.py
------------------
استخراج لینک m3u8 از 91porna.com با Playwright (مرورگر واقعی).

روش کار:
  1. صفحه‌ی detail رو در یه مرورگر واقعی (Playwright) باز می‌کنیم
  2. پلیر xgplayer با JS لینک m3u8 امضا شده (auth_key) رو درخواست می‌کنه
  3. اون درخواست رو رهگیری می‌کنیم → لینک m3u8
  4. دانلود با yt-dlp (HLS + AES-128؛ yt-dlp خودش crypt.key رو می‌گیره)

نکته‌ها:
  - لینک auth_key زمان‌داره → extract باید بلافاصله قبل از دانلود صدا زده بشه
  - هدلس (بدون نیاز به نمایشگر) — روی هر سروری کار می‌کنه
  - فقط یک استخراج/دانلود همزمان (Semaphore) برای کنترل مصرف RAM
"""

import asyncio
import html as html_lib
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("Porna91Handler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

_SITE_DOMAIN = "91porna.com"
_SITE_URL = "https://91porna.com"
_SITE_REFERER = f"{_SITE_URL}/"
_SITE_ORIGIN = _SITE_URL

_ALLOWED_HOSTS = frozenset({"91porna.com", "www.91porna.com"})

# دامنه‌های CDN رسانه (m3u8 و ts و key)
_ALLOWED_HOST_SUFFIXES = (
    ".91porna.com",
    ".ofpcif.cn",
    ".nhoqpp.cn",
)

# stealth برای دور زدن تشخیص automation
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
window.chrome = {runtime: {}};
"""

ProgressCallback = Callable[[str], Awaitable[None]]

# فقط یک استخراج/دانلود همزمان (مدیریت RAM سرور)
_SEMAPHORE = asyncio.Semaphore(1)

# الگوهای خطای توکن منقضی‌شده
_EXPIRED_PATTERNS = (
    "http error 404",
    "fragment not found",
    "403",
    "forbidden",
    "auth_key",
)


# ─── Utility ────────────────────────────────────────────────


def is_91porna_url(url: str) -> bool:
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


def _check_playwright() -> bool:
    try:
        import playwright  # noqa: F401

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


def _looks_expired(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _EXPIRED_PATTERNS)


# ─── Extraction با Playwright ───────────────────────────────


async def _extract_m3u8_via_browser(url: str) -> Tuple[Optional[str], str]:
    """لینک m3u8 رو با باز کردن صفحه در مرورگر واقعی رهگیری می‌کنه."""
    from playwright.async_api import async_playwright

    found: List[str] = []
    title = "Untitled"

    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--mute-audio",
                    "--disable-gpu",
                    "--single-process",
                ],
                ignore_default_args=["--enable-automation"],
                handle_sigint=False,
                handle_sigterm=False,
            )
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="zh-CN",
            )
            await context.add_init_script(_STEALTH_JS)
            page = await context.new_page()

            def on_request(req):
                u = req.url
                if ".m3u8" in u and _is_allowed_host(u):
                    found.append(u)

            page.on("request", on_request)

            await page.goto(url, wait_until="load", timeout=40000)
            await page.wait_for_timeout(2500)

            try:
                title = (await page.title()) or "Untitled"
                title = re.sub(r"\s*[-|].*在线观看.*$", "", title).strip()
                title = html_lib.unescape(title) or "Untitled"
            except Exception:
                pass

            if not found:
                try:
                    cont = await page.query_selector(
                        ".player, #player, [class*=xgplayer], [class*=player]"
                    )
                    if cont:
                        box = await cont.bounding_box()
                        if box:
                            await page.mouse.click(
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                except Exception:
                    pass

            for _ in range(25):
                if found:
                    break
                await page.wait_for_timeout(1000)
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass

    if found:
        # ترجیح master playlist (معمولاً شامل کلمه master یا کوتاه‌ترین مسیر)
        master = next((u for u in found if "master" in u.lower()), None)
        return (master or found[0]), title
    return None, title


async def extract_91porna_qualities(url: str) -> Tuple[List[dict], str]:
    """لینک m3u8 رو از 91porna استخراج می‌کنه (با Playwright)."""
    if not is_91porna_url(url):
        return [], "Invalid URL"

    if not _check_playwright():
        return (
            [],
            "playwright لازمه: pip install playwright && playwright install chromium",
        )

    async with _SEMAPHORE:
        try:
            m3u8_url, title = await _extract_m3u8_via_browser(url)
        except Exception as e:
            logger.exception("Playwright extraction failed")
            return [], f"Browser extraction failed: {str(e)[:120]}"
        finally:
            await asyncio.sleep(1.0)

    if not m3u8_url:
        return [], "m3u8 not found (پلیر لود نشد یا توکن گرفته نشد)"

    qualities = [
        {
            "label": "📡 دانلود (HLS)",
            "url": m3u8_url,
            "method": "m3u8",
            # آدرس صفحه اصلی رو نگه می‌داریم تا اگه توکن منقضی شد، دوباره extract کنیم
            "page_url": url,
        }
    ]
    logger.info("Extracted m3u8 for: %s", title[:60])
    return qualities, title


# ─── Download (yt-dlp) ──────────────────────────────────────


async def _download_with_ytdlp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
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
            # ── retry کوتاه: روی توکن منقضی سریع fail کن، دقایق هنگ نکن ──
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--retry-sleep",
            "fragment:linear=1:3:1",
            "--abort-on-unavailable-fragment",
            "--buffer-size",
            "16K",
            "--max-filesize",
            str(MAX_DOWNLOAD_SIZE),
            "--add-header",
            f"Referer:{_SITE_REFERER}",
            "--add-header",
            f"Origin:{_SITE_ORIGIN}",
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
        cmd.append(url)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        last_update = 0.0
        tail: List[str] = []
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
            if text:
                tail.append(text)
                if len(tail) > 15:
                    tail.pop(0)
            now = time.time()
            if now - last_update >= 2.0 and text:
                last_update = now
                await progress_cb(f"📥 **Downloading...**\n`{text[:80]}`")

        await process.wait()
        if process.returncode != 0:
            full_err = "\n".join(tail)
            # تشخیص توکن منقضی‌شده → پیام واضح
            if _looks_expired(full_err):
                return False, "__EXPIRED__", 0
            err = "\n".join(tail[-5:]) or "yt-dlp failed"
            return False, err[:200], 0

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


async def download_91porna_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    page_url: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود HLS stream از 91porna با yt-dlp.

    اگه توکن منقضی شده باشه و page_url داده شده باشه، یک بار به‌صورت
    خودکار دوباره extract می‌کنه و با لینک تازه دوباره تلاش می‌کنه.
    """
    if not _is_allowed_host(m3u8_url):
        return False, "URL host not allowed", 0

    async with _SEMAPHORE:
        success, error, size = await _download_with_ytdlp(
            m3u8_url, filepath, progress_cb
        )

    if success:
        return True, "", size

    _cleanup_file(filepath)

    # ── توکن منقضی شده: یک بار رفرش خودکار لینک و تلاش مجدد ──
    if error == "__EXPIRED__" and page_url and is_91porna_url(page_url):
        await progress_cb("♻️ **توکن منقضی شد، در حال گرفتن لینک تازه...**")
        qualities, _title = await extract_91porna_qualities(page_url)
        if qualities:
            fresh_url = qualities[0]["url"]
            async with _SEMAPHORE:
                success, error, size = await _download_with_ytdlp(
                    fresh_url, filepath, progress_cb
                )
            if success:
                return True, "", size
            _cleanup_file(filepath)

    if error == "__EXPIRED__":
        error = (
            "لینک منقضی شد (توکن زمان‌دار). لطفاً دوباره لینک ویدیو رو بفرست "
            "تا از نو استخراج بشه."
        )

    return False, error, 0


async def download_91porna_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    page_url: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """دانلود مستقیم (برای سازگاری با API دیگر handlerها)."""
    return await download_91porna_m3u8(url, filepath, progress_cb, page_url)
