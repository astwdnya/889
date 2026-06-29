"""
rat_handler.py
--------------
استخراج لینک‌های دانلود از rat.xxx (پلتفرم KVS / Kernel Video Sharing).

روش کار:
  - سایت مبتنی بر KVS هست
  - flashvars شامل video_url با پیشوند 'function/0/' و license_code هست
  - مسیر get_file با الگوریتم KVS و license_code رمزگشایی میشه
  - چند کیفیت: video_url (360p), video_alt_url (480p), video_alt_url2 (720p)
"""

import asyncio
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("RatHandler")

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

_SITE_DOMAIN = "rat.xxx"
_SITE_URL = "https://www.rat.xxx"
_SITE_REFERER = f"{_SITE_URL}/"

_ALLOWED_HOSTS = frozenset(
    {
        "rat.xxx",
        "www.rat.xxx",
    }
)

_ALLOWED_HOST_SUFFIXES = (".rat.xxx",)

ProgressCallback = Callable[[str], Awaitable[None]]

rat_sessions: dict = {}


# ─── Utility ────────────────────────────────────────────────


def is_rat_url(url: str) -> bool:
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


# ─── KVS decoding ───────────────────────────────────────────


def _kvs_decode_url(license_code: str, url_part: str) -> str:
    """
    رمزگشایی URL با الگوریتم KVS.

    ورودی url_part: بخش بعد از 'function/0/' (مسیر get_file رمزشده)
    license_code: مثل '$310817210254405'

    خروجی: URL کامل و معتبر.
    """
    # پیدا کردن بخش رمزشده (۳۲ کاراکتر بعد از آخرین get_file segment)
    # ساختار: .../get_file/N/<32hex_scrambled><rest>/...
    # بخش scrambled همون ۳۲ کاراکتر اول قطعه‌ی hash هست.
    parts = url_part.split("/")
    # پیدا کردن قطعه‌ای که >=32 کاراکتر hex داره
    idx = None
    for i, seg in enumerate(parts):
        if len(seg) >= 32 and re.fullmatch(r"[0-9a-f]+", seg[:32]):
            idx = i
            break
    if idx is None:
        return url_part

    scrambled = parts[idx]
    head = scrambled[:32]
    tail = scrambled[32:]

    license_arr = _kvs_license_to_array(license_code)
    chars = list(head)

    n = len(chars)
    for k in range(n - 1, -1, -1):
        offset = license_arr[k % len(license_arr)] % n if license_arr else 0
        # swap با موقعیت محاسبه‌شده (الگوریتم استاندارد KVS)
        j = (k + offset) % n
        # نسخه‌ی رایج: جابه‌جایی دو کاراکتر
        chars[k], chars[j] = chars[j], chars[k]

    parts[idx] = "".join(chars) + tail
    return "/".join(parts)


def _kvs_license_to_array(license_code: str) -> List[int]:
    """
    تبدیل license_code به آرایه‌ی اعداد (الگوریتم KVS).
    """
    code = license_code.replace("$", "")
    if not code.isdigit():
        # اگه عددی نبود، از hash کاراکترها استفاده کن
        code = "".join(str(ord(c)) for c in code)

    result: List[int] = []
    # KVS استاندارد: جفت رقم‌ها رو می‌گیره، اگه 0 بود +1
    for i in range(0, len(code) - 1, 2):
        pair = code[i : i + 2]
        try:
            val = int(pair)
        except ValueError:
            val = 1
        result.append(val if val != 0 else 1)
    return result or [1]


def _build_quality_url(raw_value: str) -> Optional[str]:
    """
    تبدیل مقدار flashvars به URL آماده.
    اگه 'function/0/' داره، اون رو حذف کن (بعد از decode).
    """
    if not raw_value:
        return None
    val = raw_value.replace("\\/", "/").strip()
    if val.startswith("function/0/"):
        val = val[len("function/0/") :]
    if not val.startswith("http"):
        return None
    return val


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
    """استخراج بلاک flashvars به دیکشنری."""
    m = re.search(r"flashvars\s*=\s*\{(.*?)\};", html, re.DOTALL)
    if not m:
        return {}
    block = m.group(1)
    result = {}
    for fm in re.finditer(r"""['"]?([\w]+)['"]?\s*:\s*['"]([^'"]*)['"]""", block):
        result[fm.group(1)] = fm.group(2)
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
        title = re.sub(r"\s*[-|]\s*[Rr]at\.xxx.*$", "", title).strip()
        return title or "Untitled"
    return f"rat_{flashvars.get('video_id', 'video')}"


# ─── Main extraction ───────────────────────────────────────


async def extract_rat_qualities(url: str) -> Tuple[List[dict], str]:
    """
    لینک‌های کیفیت مختلف رو از صفحه rat.xxx استخراج میکنه.
    """
    if not is_rat_url(url):
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

    # نگاشت کلیدهای flashvars به برچسب کیفیت
    quality_keys = [
        ("video_url", flashvars.get("video_url_text", "360p")),
        ("video_alt_url", flashvars.get("video_alt_url_text", "480p")),
        ("video_alt_url2", flashvars.get("video_alt_url2_text", "720p")),
        ("video_alt_url3", flashvars.get("video_alt_url3_text", "1080p")),
        ("video_alt_url4", flashvars.get("video_alt_url4_text", "2160p")),
    ]

    for key, label in quality_keys:
        raw = flashvars.get(key, "")
        if not raw:
            continue

        # اگه با function/N/ شروع بشه → باید decode بشه
        decoded = raw.replace("\\/", "/").strip()
        m = re.match(r"function/\d+/(.+)", decoded)
        if m and license_code:
            inner = m.group(1)
            decoded = _kvs_decode_url(license_code, inner)

        video_url = _build_quality_url(decoded)
        if not video_url or video_url in seen:
            continue
        if not _is_allowed_host(video_url):
            logger.debug("Skipping non-allowed host: %s", video_url[:60])
            continue
        seen.add(video_url)

        qualities.append(
            {
                "label": f"🎥 {label}",
                "url": video_url,
                "method": "direct",
            }
        )

    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info("Extracted %d qualities for: %s", len(qualities), title[:60])
        return qualities, title

    return [], "no video sources found in flashvars"


# ─── Download ───────────────────────────────────────────────


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


async def download_rat_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    return False, "rat.xxx does not use m3u8 streams", 0


async def download_rat_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود لینک مستقیم MP4 از rat.xxx."""
    if not _is_allowed_host(url):
        return False, "URL host not allowed", 0

    # curl_cffi اول (برای دور زدن محافظت)
    if _check_impersonation_support():
        logger.info("Trying download with curl_cffi: %s", url[:80])
        success, error, size = await _download_with_curl_cffi(
            url, filepath, progress_cb
        )
        if success:
            return True, "", size
        logger.info("curl_cffi download failed: %s", error)
        _cleanup_file(filepath)

    # aiohttp fallback
    logger.info("Trying download with aiohttp: %s", url[:80])
    success, error, size = await _download_with_aiohttp(url, filepath, progress_cb)
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0
