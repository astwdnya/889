"""
rule34_handler.py
─────────────────
دانلود و ارسال فایل از rule34.paheal.net

روش کار:
  - rule34.xxx خودش Cloudflare داره، پس از paheal.net API استفاده میکنیم
  - لینک rule34.xxx رو تبدیل به paheal API call میکنه
  - فایل مستقیم از CDN دانلود میشه (بدون protection)
  - ویدیو، عکس و GIF ساپورت میشه
"""

import asyncio
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, quote_plus

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("Rule34Handler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "*/*",
}

# ─── Constants ──────────────────────────────────────────────

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_RETRIES = 3
RETRY_DELAY = 2.0
PROGRESS_INTERVAL = 2.0

_PAHEAL_API = "https://rule34.paheal.net/api/danbooru/find_posts"
_PAHEAL_BASE = "https://rule34.paheal.net"

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset(
    {
        "rule34.xxx",
        "www.rule34.xxx",
        "rule34.paheal.net",
        "rule34.us",
        "www.rule34.us",
    }
)

_ALLOWED_CDN_SUFFIXES = (
    ".paheal-cdn.net",
    ".paheal.net",
    ".rule34.xxx",
    ".rule34.us",
    "r34i.paheal-cdn.net",
    "r34t.paheal.net",
)

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── URL Detection ─────────────────────────────────────────


def is_rule34_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به rule34 هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".rule34.xxx")
    except Exception:
        return False


def _extract_post_id(url: str) -> Optional[int]:
    """استخراج post ID از URL."""
    # rule34.xxx: ?page=post&s=view&id=17922670
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "id" in params:
        try:
            return int(params["id"][0])
        except (ValueError, IndexError):
            pass

    # rule34.paheal.net: /post/view/12345
    m = re.search(r"/post/view/(\d+)", url)
    if m:
        return int(m.group(1))

    # rule34.us: ?r=posts/view&id=12345
    if "id" in params:
        try:
            return int(params["id"][0])
        except (ValueError, IndexError):
            pass

    return None


def _extract_tags(url: str) -> str:
    """استخراج tags از URL (اگه بود)."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    tags = params.get("tags", params.get("q", [""]))[0]
    return tags.replace("+", " ").strip()


def _is_allowed_cdn(url: str) -> bool:
    """بررسی CDN مجاز."""
    try:
        host = urlparse(url).hostname or ""
        return any(
            host.endswith(s) or host == s.lstrip(".") for s in _ALLOWED_CDN_SUFFIXES
        )
    except Exception:
        return False


# ─── Utility ────────────────────────────────────────────────


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning("Failed to cleanup %s: %s", filepath, e)


def _format_progress(
    downloaded: int, content_length: int, start_time: float, now: float
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
        f"📥 **Downloading...**\n"
        f"💾 {dl_mb:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
    )


def _detect_media_type(file_url: str) -> str:
    """تشخیص نوع مدیا."""
    lower = file_url.lower()
    if any(ext in lower for ext in [".mp4", ".webm", ".mov"]):
        return "video"
    elif ".gif" in lower:
        return "gif"
    else:
        return "image"


def _make_title(tags: str, post_id: str) -> str:
    """ساخت عنوان از tags."""
    skip = {
        "animated",
        "video",
        "sound",
        "tagme",
        "webm",
        "mp4",
        "gif",
        "loop",
        "has_audio",
        "no_audio",
    }
    parts = [t for t in tags.split() if t.lower() not in skip]
    if parts:
        return " ".join(parts[:6]).replace("_", " ").title()
    return f"Rule34 #{post_id}"


# ─── HTTP ───────────────────────────────────────────────────


async def _fetch(url: str, timeout_sec: int = 15) -> Optional[str]:
    """دریافت URL."""
    timeout = ClientTimeout(total=timeout_sec, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=_HEADERS, allow_redirects=True) as resp:
                if resp.status == 200:
                    return await resp.text(errors="replace")
                logger.warning("HTTP %d for %s", resp.status, url)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Fetch failed: %s", e)
    return None


# ─── Extraction ────────────────────────────────────────────


async def extract_rule34_post(
    url: str,
    debug_cb: Optional[ProgressCallback] = None,
) -> Tuple[Optional[dict], str]:
    """
    استخراج اطلاعات پست از rule34.

    Returns:
        (post_dict, error_message)
    """
    if not is_rule34_url(url):
        return None, "Invalid URL"

    post_id = _extract_post_id(url)
    if not post_id:
        return None, "Could not extract post ID from URL"

    host = urlparse(url).hostname or ""

    if debug_cb:
        await debug_cb(f"🔍 Fetching post #{post_id}...")

    if "rule34.xxx" in host:
        post = await _extract_from_rule34xxx(post_id, debug_cb)
        if post:
            return post, ""

        if debug_cb:
            await debug_cb("🔍 Trying rule34.us...")
        post = await _extract_from_rule34us(post_id, debug_cb)
        if post:
            return post, ""

    elif "paheal" in host:
        post = await _extract_from_paheal(post_id, debug_cb)
        if post:
            return post, ""

    elif "rule34.us" in host:
        post = await _extract_from_rule34us(post_id, debug_cb)
        if post:
            return post, ""

    else:
        for method in [
            _extract_from_rule34xxx,
            _extract_from_rule34us,
            _extract_from_paheal,
        ]:
            post = await method(post_id, debug_cb)
            if post:
                return post, ""

    return None, f"Could not find post #{post_id}"


async def _extract_from_rule34xxx(
    post_id: int,
    debug_cb: Optional[ProgressCallback] = None,
) -> Optional[dict]:
    """استخراج از rule34.xxx با curl_cffi (bypass Cloudflare)."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        logger.warning("curl_cffi not installed, skipping rule34.xxx")
        return None

    page_url = f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}"

    if debug_cb:
        await debug_cb("🔍 Fetching from rule34.xxx (curl_cffi)...")

    try:
        async with AsyncSession() as session:
            try:
                await session.get(
                    "https://rule34.xxx/",
                    impersonate="chrome",
                    timeout=15,
                )
            except Exception:
                pass

            resp = await session.get(
                page_url,
                impersonate="chrome",
                headers={
                    "Referer": "https://rule34.xxx/",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                allow_redirects=True,
                timeout=30,
            )

            if resp.status_code != 200:
                logger.info("rule34.xxx returned HTTP %d", resp.status_code)
                return None

            html = resp.text
            return _parse_rule34xxx_page(html, post_id)

    except Exception as e:
        logger.warning("rule34.xxx extraction failed: %s", e)
        return None


def _parse_rule34xxx_page(html: str, post_id: int) -> Optional[dict]:
    """پارس صفحه پست rule34.xxx."""
    file_url = None

    m = re.search(r'<source[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        file_url = m.group(1)

    if not file_url:
        m = re.search(
            r'<img[^>]+id=["\']image["\'][^>]+src=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if m:
            file_url = m.group(1)

    if not file_url:
        m = re.search(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*Original\s*(?:image|file)',
            html,
            re.IGNORECASE,
        )
        if m:
            file_url = m.group(1)

    if not file_url:
        m = re.search(
            r'<a[^>]+id=["\']highres["\'][^>]+href=["\']([^"\']+)["\']',
            html,
            re.IGNORECASE,
        )
        if not m:
            m = re.search(
                r'href=["\']([^"\']+)["\'][^>]+id=["\']highres["\']',
                html,
                re.IGNORECASE,
            )
        if m:
            file_url = m.group(1)

    if not file_url:
        m = re.search(
            r'["\']([^"\']*(?:rule34|r34|wimg)[^"\']*\.(?:mp4|webm|gif|jpg|png)[^"\']*)["\']',
            html,
        )
        if m:
            file_url = m.group(1)

    if not file_url:
        logger.info("No file URL found in rule34.xxx page for #%d", post_id)
        return None

    if file_url.startswith("//"):
        file_url = "https:" + file_url
    elif file_url.startswith("/"):
        file_url = "https://rule34.xxx" + file_url

    tags = ""
    tag_section = re.search(
        r'id=["\']tag-sidebar["\'][^>]*>(.*?)</ul>', html, re.DOTALL | re.IGNORECASE
    )
    if tag_section:
        tag_matches = re.findall(
            r'class=["\'][^"\']*tag-type[^"\']*["\'][^>]*>.*?<a[^>]*>([^<]+)</a>',
            tag_section.group(1),
            re.DOTALL,
        )
        if tag_matches:
            tags = " ".join(t.strip() for t in tag_matches)

    if not tags:
        tag_matches = re.findall(
            r'href=["\'][^"\']*[?&]tags=([^"\'&]+)["\']',
            html,
        )
        if tag_matches:
            unique_tags = list(dict.fromkeys(tag_matches))
            tags = " ".join(t.replace("+", " ") for t in unique_tags[:20])

    media_type = _detect_media_type(file_url)
    title = _make_title(tags, str(post_id))

    preview = ""
    pm = re.search(
        r'property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not pm:
        pm = re.search(
            r'content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            html,
            re.IGNORECASE,
        )
    if pm:
        preview = pm.group(1)

    return {
        "file_url": file_url,
        "preview_url": preview,
        "tags": tags,
        "title": title,
        "media_type": media_type,
        "post_id": str(post_id),
        "post_url": f"https://rule34.xxx/index.php?page=post&s=view&id={post_id}",
        "width": 0,
        "height": 0,
        "score": "0",
    }


async def _extract_from_paheal(
    post_id: int,
    debug_cb: Optional[ProgressCallback] = None,
) -> Optional[dict]:
    """استخراج از paheal.net."""
    if debug_cb:
        await debug_cb("🔍 Trying paheal.net...")

    post_url = f"{_PAHEAL_BASE}/post/view/{post_id}"
    html = await _fetch(post_url)
    if not html:
        return None

    return _parse_paheal_post_page(html, post_id)


async def _extract_from_rule34us(
    post_id: int,
    debug_cb: Optional[ProgressCallback] = None,
) -> Optional[dict]:
    """استخراج از rule34.us."""
    if debug_cb:
        await debug_cb("🔍 Trying rule34.us...")

    r34us_url = f"https://rule34.us/index.php?r=posts/view&id={post_id}"
    html = await _fetch(r34us_url)
    if not html:
        return None

    return _parse_rule34us_post(html, post_id)


# ─── Parsers ───────────────────────────────────────────────


def _parse_paheal_post_page(html: str, post_id: int) -> Optional[dict]:
    """پارس صفحه پست paheal.net."""
    # file URL - چند الگو
    file_url = None

    # الگو 1: لینک File/Download
    m = re.search(
        r"href=['\"]([^'\"]+)['\"][^>]*>\s*(?:File|Download|Original)",
        html,
        re.IGNORECASE,
    )
    if m:
        file_url = m.group(1)

    # الگو 2: source tag
    if not file_url:
        m = re.search(r"<source[^>]+src=['\"]([^'\"]+)['\"]", html, re.IGNORECASE)
        if m:
            file_url = m.group(1)

    # الگو 3: main_image
    if not file_url:
        m = re.search(
            r"<img[^>]+id=['\"]main_image['\"][^>]+src=['\"]([^'\"]+)['\"]",
            html,
            re.IGNORECASE,
        )
        if m:
            file_url = m.group(1)

    # الگو 4: هر لینک CDN
    if not file_url:
        m = re.search(r"['\"]([^'\"]*paheal-cdn\.net[^'\"]+)['\"]", html)
        if m:
            file_url = m.group(1)

    if not file_url:
        return None

    # tags
    tag_matches = re.findall(
        r"class=['\"][^'\"]*tag_name[^'\"]*['\"][^>]*>([^<]+)<",
        html,
    )
    if not tag_matches:
        tag_matches = re.findall(
            r"/post/list/([^/\"']+)/1",
            html,
        )
    tags = " ".join(tag_matches) if tag_matches else ""

    # preview
    preview = ""
    pm = re.search(
        r"<img[^>]+id=['\"]thumb['\"][^>]+src=['\"]([^'\"]+)['\"]", html, re.IGNORECASE
    )
    if pm:
        preview = pm.group(1)

    media_type = _detect_media_type(file_url)
    title = _make_title(tags, str(post_id))

    return {
        "file_url": file_url,
        "preview_url": preview,
        "tags": tags,
        "title": title,
        "media_type": media_type,
        "post_id": str(post_id),
        "post_url": f"{_PAHEAL_BASE}/post/view/{post_id}",
        "width": 0,
        "height": 0,
        "score": "0",
    }


def _parse_paheal_api(xml_text: str) -> List[dict]:
    """پارس XML response از paheal API."""
    posts = []

    try:
        root = ET.fromstring(xml_text)
        elements = root.findall("tag")
    except ET.ParseError:
        # fallback regex
        elements = None

    if elements:
        for elem in elements:
            file_url = elem.get("file_url", "")
            if not file_url:
                continue

            tags = elem.get("tags", "")
            post_id = elem.get("id", "")
            media_type = _detect_media_type(file_url)

            try:
                width = int(elem.get("width", 0))
                height = int(elem.get("height", 0))
            except (ValueError, TypeError):
                width, height = 0, 0

            posts.append(
                {
                    "file_url": file_url,
                    "preview_url": elem.get("preview_url", ""),
                    "tags": tags,
                    "title": _make_title(tags, post_id),
                    "media_type": media_type,
                    "post_id": post_id,
                    "post_url": f"{_PAHEAL_BASE}/post/view/{post_id}",
                    "width": width,
                    "height": height,
                    "score": elem.get("score", "0"),
                }
            )
    else:
        # regex fallback
        for m in re.finditer(r"<tag\s+([^>]+?)/?>", xml_text, re.DOTALL):
            attrs = {}
            for am in re.finditer(r"(\w+)='([^']*)'", m.group(1)):
                attrs[am.group(1)] = am.group(2)

            file_url = attrs.get("file_url", "")
            if not file_url:
                continue

            tags = attrs.get("tags", "")
            post_id = attrs.get("id", "")
            media_type = _detect_media_type(file_url)

            posts.append(
                {
                    "file_url": file_url,
                    "preview_url": attrs.get("preview_url", ""),
                    "tags": tags,
                    "title": _make_title(tags, post_id),
                    "media_type": media_type,
                    "post_id": post_id,
                    "post_url": f"{_PAHEAL_BASE}/post/view/{post_id}",
                    "width": int(attrs.get("width", 0)),
                    "height": int(attrs.get("height", 0)),
                    "score": attrs.get("score", "0"),
                }
            )

    return posts


def _parse_rule34us_post(html: str, post_id: int) -> Optional[dict]:
    """پارس صفحه پست rule34.us."""
    file_url = None

    # video source
    m = re.search(r"<source[^>]+src=['\"]([^'\"]+)['\"]", html, re.IGNORECASE)
    if m:
        file_url = m.group(1)

    # content image
    if not file_url:
        m = re.search(
            r"<img[^>]+(?:id=['\"](?:image|content_image)['\"]|class=['\"][^'\"]*content[^'\"]*['\"])[^>]+src=['\"]([^'\"]+)['\"]",
            html,
            re.IGNORECASE,
        )
        if m:
            file_url = m.group(1)

    # any rule34.us media URL
    if not file_url:
        m = re.search(
            r"['\"]([^'\"]*rule34\.us[^'\"]*\.(?:mp4|webm|gif|jpg|png)[^'\"]*)['\"]",
            html,
        )
        if m:
            file_url = m.group(1)

    if not file_url:
        return None

    # tags
    tag_matches = re.findall(r"class=['\"][^'\"]*tag[^'\"]*['\"][^>]*>([^<]+)<", html)
    tags = (
        " ".join(t.strip() for t in tag_matches if len(t.strip()) > 1)
        if tag_matches
        else ""
    )

    media_type = _detect_media_type(file_url)
    title = _make_title(tags, str(post_id))

    return {
        "file_url": file_url,
        "preview_url": "",
        "tags": tags,
        "title": title,
        "media_type": media_type,
        "post_id": str(post_id),
        "post_url": f"https://rule34.us/index.php?r=posts/view&id={post_id}",
        "width": 0,
        "height": 0,
        "score": "0",
    }


# ─── Download ──────────────────────────────────────────────


async def download_rule34(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """
    دانلود فایل از rule34 CDN.
    CDN بدون protection هست پس aiohttp ساده کافیه.
    """
    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers={
                        **_HEADERS,
                        "Referer": "https://rule34.paheal.net/",
                    },
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if 400 <= resp.status < 500 and resp.status != 403:
                            return False, error, 0
                        continue

                    content_length = int(resp.headers.get("Content-Length", 0))
                    if content_length > MAX_DOWNLOAD_SIZE:
                        return (
                            False,
                            f"File too large: {content_length / 1024 / 1024:.0f} MB",
                            0,
                        )

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
                            if now - last_update >= PROGRESS_INTERVAL:
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
