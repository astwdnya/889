"""
pornzog_handler.py
------------------
استخراج و دانلود از pornzog.com (aggregator → videotxxx.com).

روش کار:
  1. صفحه pornzog → iframe videotxxx.com/embed/<id> رو پیدا می‌کنیم
  2. video_id → API: videotxxx.com/api/videofile.php?video_id=<id>
  3. JSON یه video_url رمزشده داره (base64 + obfuscation سیریلیک):
     کاراکترهای سیریلیک (М,А,С...) جای حروف لاتین مشابه نشستن
  4. decode → لینک get_file → redirect به txxx.com → CDN (ahcdn)
  5. دانلود مستقیم با aiohttp؛ کلید: Referer باید https://txxx.com/ باشه

نکته: توکن ti زمان‌داره → extract باید بلافاصله قبل از دانلود صدا زده بشه.
"""

import asyncio
import base64
import html as html_lib
import json
import logging
import os
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("PornzogHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
SESSION_TTL = 30 * 60
MAX_RETRIES = 3
RETRY_DELAY = 2.0

_SITE_DOMAIN = "pornzog.com"
_SITE_URL = "https://pornzog.com"
_SITE_REFERER = f"{_SITE_URL}/"

# دامنه‌ی host واقعی ویدیو
_VIDEO_HOST = "https://videotxxx.com"
# Referer لازم برای CDN نهایی (کلید حل مشکل!)
_CDN_REFERER = "https://txxx.com/"

_ALLOWED_HOSTS = frozenset({"pornzog.com", "www.pornzog.com"})

# دامنه‌های مجاز برای host واقعی ویدیو و CDN
_ALLOWED_HOST_SUFFIXES = (
    ".pornzog.com",
    ".videotxxx.com",
    "videotxxx.com",
    ".txxx.com",
    "txxx.com",
    ".ahcdn.com",
)

# نگاشت نام یونی‌کد حروف سیریلیک → لاتین مشابه ظاهری (obfuscation KVS)
_CYRILLIC_NAME_MAP = {
    "EM": "M",
    "A": "A",
    "ES": "C",
    "IE": "E",
    "O": "O",
    "ER": "P",
    "TE": "T",
    "EN": "H",
    "KA": "K",
    "VE": "B",
    "HA": "X",
    "U": "Y",
    "EL": "L",
    "DE": "D",
    "I": "I",
    "BE": "B",
    "GHE": "G",
    "ZE": "3",
    "EF": "F",
    "SHA": "W",
}

# الگوی استخراج video_id از iframe videotxxx
_VT_EMBED_RE = re.compile(r"(?:videotxxx\.com|txxx\.com)/embed/(\d+)", re.I)

pornzog_sessions: Dict[str, dict] = {}

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def _is_allowed_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or any(
            host == s or host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES
        )
    except Exception:
        return False


def is_pornzog_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(f".{_SITE_DOMAIN}")
    except Exception:
        return False


def cleanup_expired_sessions() -> int:
    now = time.time()
    expired = [
        sid
        for sid, data in pornzog_sessions.items()
        if now - data.get("created_at", 0) > SESSION_TTL
    ]
    for sid in expired:
        pornzog_sessions.pop(sid, None)
    return len(expired)


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _cyrillic_to_latin(ch: str) -> str:
    if ord(ch) < 128:
        return ch
    name = unicodedata.name(ch, "")
    m = re.search(r"LETTER (\w+)$", name)
    if m:
        return _CYRILLIC_NAME_MAP.get(m.group(1), "")
    return ""


def _decode_video_url(raw: str) -> Optional[str]:
    try:
        mapped = "".join(_cyrillic_to_latin(c) for c in raw)
        mapped = mapped.replace("~", "=")
        pad = len(mapped) % 4
        if pad:
            mapped += "=" * (4 - pad)
        decoded = base64.b64decode(mapped).decode("utf-8", errors="replace")
        m = re.search(r"(/get_file/\S+|https?://\S+)", decoded)
        if m:
            return m.group(1)
        if decoded.startswith("/"):
            return decoded
        logger.warning("decoded but no path: %s", decoded[:80])
        return None
    except Exception as e:
        logger.warning("decode video_url failed: %s", e)
        return None


def _quality_from_format(fmt: str) -> Tuple[str, int]:
    """از format مثل '_hq.mp4' یا '_360p.mp4' کیفیت رو درمیاره."""
    m = re.search(r"(\d+)p", fmt)
    if m:
        h = int(m.group(1))
        return f"🎥 {h}p", h
    if "hq" in fmt.lower():
        return "🎥 HQ", 720
    if "lq" in fmt.lower():
        return "🎥 LQ", 240
    return "🎥 MP4", 0


# ─── HTTP helpers (curl_cffi برای دور زدن Cloudflare) ────────


def _check_impersonation_support() -> bool:
    try:
        import curl_cffi  # noqa: F401

        return True
    except ImportError:
        return False


async def _cffi_get(session, url: str, referer: str, ajax: bool = False):
    headers = {"Referer": referer}
    if ajax:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return await session.get(url, impersonate="chrome", headers=headers, timeout=25)


# ─── Extraction ─────────────────────────────────────────────


def _extract_title(html: str) -> str:
    m = re.search(
        r'<meta[^>]+og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
    )
    if m:
        return html_lib.unescape(m.group(1).strip()) or "Untitled"
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*PornZog.*$", "", title, flags=re.I).strip()
        return html_lib.unescape(title) or "Untitled"
    return "Untitled"


def _find_videotxxx_id(html: str) -> Optional[str]:
    """video_id رو از iframe videotxxx در صفحه pornzog پیدا می‌کنه."""
    m = _VT_EMBED_RE.search(html)
    return m.group(1) if m else None


async def extract_pornzog_qualities(
    url: str,
    debug_cb: Optional[Callable[[str], None]] = None,
) -> Tuple[List[dict], str]:
    """لینک‌های ویدیو رو از pornzog (via videotxxx) استخراج می‌کنه."""
    if not is_pornzog_url(url):
        return [], "Invalid URL"

    if not _check_impersonation_support():
        return [], "curl_cffi لازمه: pip install curl_cffi"

    def dbg(msg: str) -> None:
        logger.info("[PZ-DEBUG] %s", msg)
        if debug_cb:
            debug_cb(msg)

    cleanup_expired_sessions()

    from curl_cffi.requests import AsyncSession

    async with AsyncSession() as s:
        dbg("📡 Step 1: Fetching pornzog page...")
        try:
            r = await _cffi_get(s, url, _SITE_REFERER)
            html = r.text
            dbg(f"✅ Page fetched, size={len(html)} bytes")
        except Exception as e:
            dbg(f"❌ Fetch failed: {e}")
            return [], f"Could not fetch page: {str(e)[:80]}"

        title = _extract_title(html)
        dbg(f"🏷 Title: {title[:60]}")

        dbg("🔍 Step 2: Looking for videotxxx iframe...")
        video_id = _find_videotxxx_id(html)
        dbg(f"video_id: {video_id}")
        if not video_id:
            dbg("❌ No iframe found")
            return [], "iframe videotxxx پیدا نشد (ساختار سایت تغییر کرده؟)"

        embed_url = f"{_VIDEO_HOST}/embed/{video_id}/"
        dbg(f"🌐 Step 3: Visiting embed: {embed_url}")

        try:
            await _cffi_get(s, embed_url, url)
            dbg("✅ Embed visited (cookies set)")
        except Exception as e:
            dbg(f"⚠️ Embed visit failed (non-fatal): {e}")

        dbg("📦 Step 4: Calling API...")
        api_url = f"{_VIDEO_HOST}/api/videofile.php?video_id={video_id}"
        dbg(f"API URL: {api_url}")
        try:
            r = await _cffi_get(s, api_url, embed_url, ajax=True)
            api_text = r.text
            dbg(f"✅ API response ({len(api_text)} chars):\n{api_text[:500]}")
            data = json.loads(api_text)
        except Exception as e:
            dbg(f"❌ API failed: {e}")
            logger.warning("API videofile failed: %s", e)
            return [], f"API videofile failed: {str(e)[:80]}"

        if not isinstance(data, list):
            data = [data]

        dbg(f"📊 API data items: {len(data)}")

        qualities: List[dict] = []
        seen = set()

        for i, item in enumerate(data):
            if not isinstance(item, dict):
                dbg(f"  item[{i}]: not a dict, skipping")
                continue
            raw = item.get("video_url") or ""
            fmt = item.get("format") or ""
            dbg(f"  item[{i}]: format={fmt}, raw_video_url (first 120)={raw[:120]}")
            if not raw:
                dbg(f"  item[{i}]: no video_url, skipping")
                continue

            non_ascii = {c for c in raw if ord(c) >= 128}
            if non_ascii:
                names = [(c, unicodedata.name(c, "?")) for c in non_ascii]
                logger.info("video_url non-ASCII chars: %s", names)
                dbg(f"  non-ASCII chars: {names}")
            else:
                logger.info("video_url (all ASCII): %s", raw[:120])
                dbg(f"  all ASCII")
            path = _decode_video_url(raw)
            if not path:
                logger.warning("decode failed for raw (first 100): %s", raw[:100])
                dbg(f"❌ decode FAILED (raw={raw[:100]})")
                continue

            full = path if path.startswith("http") else _VIDEO_HOST + path
            dbg(f"  decoded path: {full[:100]}")
            if full in seen:
                dbg(f"  duplicate, skipping")
                continue
            seen.add(full)

            if not _is_allowed_host(full):
                logger.warning("Blocked decoded host: %s", full[:60])
                dbg(f"❌ host not allowed: {full[:60]}")
                continue

            label, height = _quality_from_format(fmt)
            dbg(f"✅ quality: {label} (height={height})")
            qualities.append(
                {
                    "label": label,
                    "url": full,
                    "method": "direct",
                    "height": height,
                    "page_url": url,
                }
            )

        if not qualities:
            dbg("❌ No qualities extracted at all")
            return [], "لینک ویدیو پیدا نشد (decode ناموفق؟)"

        qualities.sort(key=lambda q: q.get("height", 0), reverse=True)
        logger.info("Extracted %d qualities for: %s", len(qualities), title[:60])
        dbg(f"✅ Total qualities: {len(qualities)}")
        return qualities, title


# ─── Download: Direct (aiohttp + progress) ──────────────────


@asynccontextmanager
async def _get_session(timeout: Optional[ClientTimeout] = None):
    t = timeout or ClientTimeout(total=3600, connect=30, sock_read=120)
    session = aiohttp.ClientSession(timeout=t, headers=_DEFAULT_HEADERS)
    try:
        yield session
    finally:
        await session.close()


def _format_progress(downloaded, content_length, start_time, now) -> str:
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
        f"📥 **Downloading...**\n"
        f"💾 {dl_mb:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
    )


async def download_pornzog_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    page_url: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود مستقیم با aiohttp.

    کلید حل مشکل: Referer باید https://txxx.com/ باشه (دامنه نهایی CDN).
    اگه توکن منقضی شد و page_url موجوده، یک بار خودکار رفرش می‌کنه.
    """
    if not _is_allowed_host(url):
        return False, "URL host not allowed", 0

    # Referer حیاتی: txxx.com (نه videotxxx، نه ahcdn)
    headers = {**_DEFAULT_HEADERS, "Referer": _CDN_REFERER}

    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            success, error, size = await _do_direct_download(
                url, filepath, headers, progress_cb
            )
            if success:
                return True, "", size

            # 4xx = توکن منقضی یا referer غلط → retry بی‌فایده
            if error.startswith("HTTP 4"):
                break
        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            error = str(e)[:150]
            logger.warning(
                "Download attempt %d/%d failed: %s", attempt, MAX_RETRIES, error
            )

        if attempt < MAX_RETRIES:
            _cleanup_file(filepath)
            await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)

    # ── رفرش خودکار توکن ──
    if page_url and is_pornzog_url(page_url):
        await progress_cb("♻️ **توکن منقضی شد، در حال گرفتن لینک تازه...**")
        qualities, _t = await extract_pornzog_qualities(page_url)
        if qualities:
            # نزدیک‌ترین کیفیت به همون لینک (یا بهترین)
            fresh_url = qualities[0]["url"]
            try:
                success, err2, size = await _do_direct_download(
                    fresh_url, filepath, headers, progress_cb
                )
                if success:
                    return True, "", size
                error = err2
            except Exception as e:
                error = str(e)[:150]
            _cleanup_file(filepath)

    if error.startswith("HTTP 4") or "Wrong referer" in error:
        error = "لینک منقضی شد. لطفاً دوباره لینک ویدیو رو بفرست."

    return False, error, 0


async def _do_direct_download(
    url: str,
    filepath: str,
    headers: dict,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    timeout = ClientTimeout(total=3600, connect=30, sock_read=120)

    async with _get_session(timeout) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status != 200:
                # body کوتاه ممکنه 'Wrong referer' باشه
                body = await resp.content.read(64)
                if b"Wrong referer" in body:
                    return False, "HTTP 403 Wrong referer", 0
                return False, f"HTTP {resp.status}", 0

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > MAX_DOWNLOAD_SIZE:
                size_mb = content_length / 1024 / 1024
                return False, f"File too large: {size_mb:.0f} MB", 0

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

    size = os.path.getsize(filepath)
    if size == 0:
        _cleanup_file(filepath)
        return False, "Downloaded file is empty", 0
    return True, "", size


# ─── سازگاری با API دیگر handlerها ──────────────────────────


async def download_pornzog_m3u8(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    page_url: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """pornzog فقط MP4 مستقیم داره؛ این برای سازگاری API هست."""
    return await download_pornzog_direct(url, filepath, progress_cb, page_url)
