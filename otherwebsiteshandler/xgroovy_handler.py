"""
xgroovy_handler.py
------------------
استخراج لینک‌های دانلود از xgroovy.com و ارسال ویدیو به کاربر.

روش کار:
  - ابتدا با request مستقیم و هدرهای کامل سعی میکنه لینک‌ها رو بگیره
  - اگه 403 گرفت، از yt-dlp --dump-json به عنوان fallback استفاده میکنه
  - لینک‌های مستقیم MP4 از HTML/JSON استخراج میشن
  - M3U8 stream ها با yt-dlp دانلود میشن
  - کاربر با دکمه کیفیت انتخاب میکنه
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("XgroovyHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}

# حداکثر حجم دانلود: 2 گیگابایت
MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

# حداکثر عمر session (ثانیه): 30 دقیقه
SESSION_TTL = 30 * 60

# حداکثر تعداد retry
MAX_RETRIES = 3
RETRY_DELAY = 2.0

# دامنه‌های مجاز
_ALLOWED_HOSTS = frozenset({
    "xgroovy.com",
    "www.xgroovy.com",
})

_ALLOWED_HOST_SUFFIXES = (
    ".xgroovy.com",
    ".xgroovy-cdn.com",
    ".gvideo.io",
    ".cdntrex.com",
    ".trafficjunky.net",
    ".googleapis.com",
    ".googleusercontent.com",
    ".cdn13.com",
    ".cdntrex.com",
    ".betacdn.net",
    ".bcdn.cc",
    ".phncdn.com",
    ".mxdcontent.net",
    ".mxplay.com",
)

# session های در حال انتظار
xgroovy_sessions: Dict[str, dict] = {}

# تایپ callback پیشرفت
ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def _is_allowed_host(url: str) -> bool:
    """بررسی اینکه URL به دامنه‌های مجاز اشاره میکنه."""
    try:
        host = urlparse(url).hostname or ""
        return (
            host in _ALLOWED_HOSTS
            or any(host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES)
        )
    except Exception:
        return False


def _is_video_cdn_url(url: str) -> bool:
    """
    بررسی گسترده‌تر برای CDN های ویدیو.
    xgroovy از CDN های متنوعی استفاده میکنه.
    """
    if _is_allowed_host(url):
        return True
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path.lower()
        if parsed.scheme == "https" and (
            path.endswith(".mp4")
            or path.endswith(".m3u8")
            or "/video/" in path
            or "/media/" in path
            or "/hls/" in path
            or "/get_file/" in path
        ):
            return True
    except Exception:
        pass
    return False


def is_xgroovy_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به xgroovy هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".xgroovy.com")
    except Exception:
        return False


def cleanup_expired_sessions() -> int:
    """پاکسازی session های منقضی شده."""
    now = time.time()
    expired = [
        sid for sid, data in xgroovy_sessions.items()
        if now - data.get("created_at", 0) > SESSION_TTL
    ]
    for sid in expired:
        xgroovy_sessions.pop(sid, None)
    if expired:
        logger.info("Cleaned up %d expired xgroovy sessions", len(expired))
    return len(expired)


def _cleanup_file(filepath: str) -> None:
    """حذف فایل اگه وجود داشته باشه."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Cleaned up file: %s", filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _normalize_url(url: str, base_url: str = "") -> Optional[str]:
    """نرمال‌سازی URL. اگه نامعتبر بود None برمیگردونه."""
    url = url.replace("\\/", "/").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http") and base_url:
        url = urljoin(base_url, url)
    elif not url.startswith("http"):
        return None
    return url


def _quality_sort_key(q: dict) -> int:
    """کلید مرتب‌سازی بر اساس عدد کیفیت."""
    nums = re.findall(r"\d+", q["label"])
    return int(nums[-1]) if nums else 0


def _format_progress(
    downloaded: int,
    content_length: int,
    start_time: float,
    now: float,
) -> str:
    """فرمت پیام پیشرفت دانلود."""
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


# ─── HTTP helpers ───────────────────────────────────────────


@asynccontextmanager
async def _get_session(timeout: Optional[ClientTimeout] = None):
    """ساخت و مدیریت aiohttp session با cookie jar و cleanup خودکار."""
    t = timeout or ClientTimeout(total=30, connect=10)
    jar = aiohttp.CookieJar(unsafe=True)
    session = aiohttp.ClientSession(
        timeout=t,
        headers=_DEFAULT_HEADERS,
        cookie_jar=jar,
    )
    try:
        yield session
    finally:
        await session.close()


async def _fetch_with_cookies(
    url: str,
    extra_headers: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
    timeout: Optional[ClientTimeout] = None,
) -> Tuple[Optional[str], int]:
    """
    دریافت محتوای URL با cookie jar و retry خودکار.
    ابتدا صفحه اصلی رو میزنه تا کوکی بگیره، بعد صفحه هدف رو.

    Returns:
        (content, status_code)
    """
    last_error = ""
    t = timeout or ClientTimeout(total=30, connect=10)

    for attempt in range(1, max_retries + 1):
        try:
            jar = aiohttp.CookieJar(unsafe=True)
            async with aiohttp.ClientSession(
                timeout=t, headers=_DEFAULT_HEADERS, cookie_jar=jar
            ) as session:
                # مرحله 1: زدن صفحه اصلی برای گرفتن کوکی
                try:
                    async with session.get(
                        "https://www.xgroovy.com/",
                        allow_redirects=True,
                    ) as home_resp:
                        # فقط کوکی‌ها رو میخوایم
                        await home_resp.read()
                        logger.debug(
                            "Homepage status: %d, cookies: %s",
                            home_resp.status,
                            list(session.cookie_jar),
                        )
                except Exception as e:
                    logger.debug("Homepage fetch failed (continuing): %s", e)

                # مرحله 2: صفحه ویدیو با کوکی‌های دریافتی
                merged = {
                    **_DEFAULT_HEADERS,
                    "Referer": "https://www.xgroovy.com/",
                    **(extra_headers or {}),
                }
                async with session.get(
                    url, headers=merged, allow_redirects=True
                ) as resp:
                    if resp.status == 200:
                        return await resp.text(errors="replace"), 200
                    last_error = f"HTTP {resp.status}"
                    if 400 <= resp.status < 500 and resp.status != 403:
                        # برای 403 retry میکنیم، بقیه 4xx نه
                        logger.warning("Client error %d for %s", resp.status, url)
                        return None, resp.status

        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = str(e)[:120]
            logger.warning(
                "Attempt %d/%d failed for %s: %s",
                attempt, max_retries, url, last_error,
            )

        if attempt < max_retries:
            await asyncio.sleep(RETRY_DELAY * attempt)

    logger.error("All %d attempts failed for %s: %s", max_retries, url, last_error)
    return None, 0


async def _fetch_with_retry(
    url: str,
    headers: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
    timeout: Optional[ClientTimeout] = None,
) -> Tuple[Optional[str], int]:
    """
    دریافت ساده URL با retry (برای CDN ها و M3U8).

    Returns:
        (content, status_code)
    """
    last_error = ""
    for attempt in range(1, max_retries + 1):
        try:
            async with _get_session(timeout) as session:
                merged = {**_DEFAULT_HEADERS, **(headers or {})}
                async with session.get(
                    url, headers=merged, allow_redirects=True
                ) as resp:
                    if resp.status == 200:
                        return await resp.text(errors="replace"), 200
                    last_error = f"HTTP {resp.status}"
                    if 400 <= resp.status < 500:
                        return None, resp.status
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = str(e)[:120]
            logger.warning(
                "Attempt %d/%d failed for %s: %s",
                attempt, max_retries, url, last_error,
            )

        if attempt < max_retries:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None, 0


# ─── yt-dlp fallback extraction ─────────────────────────────


async def _extract_with_ytdlp(url: str) -> Tuple[List[dict], str]:
    """
    استخراج لینک‌های ویدیو با yt-dlp --dump-json.
    این روش وقتی استفاده میشه که request مستقیم 403 بده.

    Returns:
        (qualities, title)
    """
    if not shutil.which("yt-dlp"):
        logger.error("yt-dlp is not installed")
        return [], "yt-dlp not installed"

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-download",
            "--dump-json",
            "--no-check-certificates",
            "-f", "best",
            url,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=60
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return [], "yt-dlp timed out"

        if process.returncode != 0:
            err = stderr.decode(errors="replace")[:200]
            logger.warning("yt-dlp --dump-json failed: %s", err)
            return [], err

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return [], "Empty yt-dlp output"

        data = json.loads(raw)
        title = data.get("title", "Untitled")
        qualities: List[dict] = []

        # فرمت‌های مختلف
        formats = data.get("formats", [])
        if not formats:
            # اگه formats نبود، لینک اصلی رو بگیر
            direct_url = data.get("url", "")
            if direct_url:
                ext = data.get("ext", "mp4")
                height = data.get("height")
                label = f"🎥 {ext.upper()} {height}p" if height else f"🎥 {ext.upper()}"
                qualities.append({
                    "label": label,
                    "url": direct_url,
                    "method": "direct" if ext != "m3u8" else "m3u8",
                })
            return qualities, title

        # پردازش فرمت‌ها
        seen_urls = set()
        for fmt in formats:
            fmt_url = fmt.get("url", "")
            if not fmt_url or fmt_url in seen_urls:
                continue
            seen_urls.add(fmt_url)

            ext = fmt.get("ext", "mp4")
            height = fmt.get("height")
            width = fmt.get("width")
            vcodec = fmt.get("vcodec", "")
            acodec = fmt.get("acodec", "")
            protocol = fmt.get("protocol", "")
            format_note = fmt.get("format_note", "")
            filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0

            # فقط فرمت‌هایی که ویدیو دارن
            if vcodec == "none":
                continue

            # تشخیص method
            is_m3u8 = (
                protocol in ("m3u8", "m3u8_native")
                or ".m3u8" in fmt_url
                or ext == "m3u8"
            )

            # ساخت label
            if height:
                size_str = f" ({filesize / 1024 / 1024:.0f}MB)" if filesize else ""
                if is_m3u8:
                    label = f"📡 M3U8 {height}p{size_str}"
                else:
                    label = f"🎥 {ext.upper()} {height}p{size_str}"
            elif format_note:
                label = f"🎥 {format_note}"
            else:
                label = f"🎥 {ext.upper()}"

            qualities.append({
                "label": label,
                "url": fmt_url,
                "method": "m3u8" if is_m3u8 else "direct",
            })

        return qualities, title

    except json.JSONDecodeError as e:
        logger.error("Failed to parse yt-dlp JSON: %s", e)
        return [], "Invalid yt-dlp output"
    except Exception as e:
        logger.exception("yt-dlp extraction error")
        return [], str(e)[:150]


# ─── Extraction ─────────────────────────────────────────────


async def extract_xgroovy_qualities(url: str) -> Tuple[List[dict], str]:
    """
    لینک‌های کیفیت مختلف رو از صفحه xgroovy استخراج میکنه.

    استراتژی:
      1. اول با request مستقیم + cookie سعی میکنه
      2. اگه 403 گرفت، از yt-dlp --dump-json استفاده میکنه

    Returns:
        (qualities, title)
    """
    if not is_xgroovy_url(url):
        logger.warning("URL is not a valid xgroovy URL: %s", url)
        return [], "Invalid URL"

    cleanup_expired_sessions()

    # ── روش 1: Request مستقیم با cookie ──
    html, status = await _fetch_with_cookies(url)

    if html is not None and status == 200:
        title = _extract_title(html)
        qualities: List[dict] = []

        _extract_from_video_tag(html, url, qualities)
        _extract_from_source_tags(html, url, qualities)
        _extract_from_json_ld(html, url, qualities)
        _extract_from_js_vars(html, url, qualities)
        _extract_from_player_config(html, url, qualities)
        await _extract_m3u8_streams(html, url, qualities)

        if qualities:
            qualities.sort(key=_quality_sort_key, reverse=True)
            logger.info(
                "Extracted %d qualities (direct) for: %s",
                len(qualities), title[:60],
            )
            return qualities, title

        # HTML گرفتیم ولی لینکی پیدا نشد، yt-dlp رو امتحان کن
        logger.info("No qualities found in HTML, falling back to yt-dlp")

    else:
        logger.info(
            "Direct fetch failed (status=%s), falling back to yt-dlp", status
        )

    # ── روش 2: yt-dlp fallback ──
    qualities, title = await _extract_with_ytdlp(url)
    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info(
            "Extracted %d qualities (yt-dlp) for: %s",
            len(qualities), title[:60],
        )
    else:
        logger.warning("No qualities found for: %s", url)

    return qualities, title


def _extract_title(html: str) -> str:
    """استخراج عنوان ویدیو از HTML."""
    # og:title
    m = re.search(
        r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if not m:
        # ترتیب attribute ها ممکنه فرق کنه
        m = re.search(
            r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:title["\']',
            html, re.IGNORECASE,
        )
    if m:
        return m.group(1).strip()

    # <title>
    m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*[Xx][Gg]roovy.*$", "", title).strip()
        return title or "Untitled"

    # h1
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return "Untitled"


def _extract_from_video_tag(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    """استخراج از تگ <video> با attribute src."""
    for m in re.finditer(
        r'<video[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE
    ):
        video_url = _normalize_url(m.group(1), page_url)
        if not video_url or not _is_video_cdn_url(video_url):
            continue
        if any(q["url"] == video_url for q in qualities):
            continue
        qualities.append({
            "label": "🎥 MP4 (video tag)",
            "url": video_url,
            "method": "direct",
        })


def _extract_from_source_tags(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    """استخراج از تگ‌های <source> داخل <video>."""
    for m in re.finditer(
        r'<source[^>]+src=["\']([^"\']+)["\']([^>]*)', html, re.IGNORECASE
    ):
        src = _normalize_url(m.group(1), page_url)
        attrs = m.group(2)
        if not src or not _is_video_cdn_url(src):
            continue
        if any(q["url"] == src for q in qualities):
            continue

        is_m3u8 = ".m3u8" in src or "application/x-mpegURL" in attrs

        # استخراج label کیفیت
        label_m = re.search(r'label=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        res_m = re.search(r'res=["\'](\d+)["\']', attrs, re.IGNORECASE)
        size_m = re.search(r'size=["\'](\d+)["\']', attrs, re.IGNORECASE)

        if label_m:
            q_label = label_m.group(1)
        elif res_m:
            q_label = f"{res_m.group(1)}p"
        elif size_m:
            q_label = f"{size_m.group(1)}p"
        else:
            url_res = re.search(r"(\d{3,4})p", src)
            q_label = f"{url_res.group(1)}p" if url_res else "Default"

        if is_m3u8:
            qualities.append({
                "label": f"📡 M3U8 {q_label}",
                "url": src,
                "method": "m3u8",
            })
        else:
            qualities.append({
                "label": f"🎥 MP4 {q_label}",
                "url": src,
                "method": "direct",
            })


def _extract_from_json_ld(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    """استخراج از JSON-LD structured data."""
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue

        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            content_url = item.get("contentUrl") or item.get("embedUrl") or ""
            if not content_url:
                continue
            video_url = _normalize_url(content_url, page_url)
            if not video_url or not _is_video_cdn_url(video_url):
                continue
            if any(q["url"] == video_url for q in qualities):
                continue
            qualities.append({
                "label": "🎥 MP4 (structured data)",
                "url": video_url,
                "method": "direct",
            })


def _extract_from_js_vars(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    """استخراج لینک ویدیو از متغیرهای JavaScript."""
    js_patterns = [
        (r"""(?:var\s+)?video_url\s*[:=]\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""(?:var\s+)?video[_]?[Ff]ile\s*[:=]\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""(?:var\s+)?video(?:Url|_src|Src|_url)\s*[:=]\s*['"]([^'"]+)['"]""", None),
        (r"""file\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""src\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""['"]?(\d{3,4})['"]?\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", "numbered"),
        # الگوی video_url با encode
        (r"""video_url\s*[:=]\s*decodeURIComponent\s*\(\s*['"]([^'"]+)['"]""", "encoded"),
    ]

    for pattern, ptype in js_patterns:
        for m in re.finditer(pattern, html):
            if ptype == "numbered":
                quality_num = m.group(1)
                raw_url = m.group(2)
                label = f"🎥 MP4 {quality_num}p"
            elif ptype == "encoded":
                from urllib.parse import unquote
                raw_url = unquote(m.group(1))
                url_res = re.search(r"(\d{3,4})p", raw_url)
                label = f"🎥 MP4 {url_res.group(1)}p" if url_res else "🎥 MP4"
            else:
                raw_url = m.group(1)
                url_res = re.search(r"(\d{3,4})p", raw_url)
                label = f"🎥 MP4 {url_res.group(1)}p" if url_res else "🎥 MP4"

            video_url = _normalize_url(raw_url, page_url)
            if not video_url or not _is_video_cdn_url(video_url):
                continue
            if any(q["url"] == video_url for q in qualities):
                continue
            qualities.append({
                "label": label,
                "url": video_url,
                "method": "direct",
            })


def _extract_from_player_config(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    """استخراج از JSON config پلیر."""
    config_patterns = [
        r"(?:flashvars|playerConfig|videoConfig|player_config)\s*=\s*(\{[^;]+\})",
        r"(?:flashvars|playerConfig|videoConfig)\s*=\s*'(\{[^']+\})'",
        r'data-config=["\'](\{[^"\']+\})["\']',
    ]

    for pattern in config_patterns:
        for m in re.finditer(pattern, html, re.DOTALL):
            raw_json = m.group(1)
            raw_json = re.sub(r"'", '"', raw_json)
            try:
                config = json.loads(raw_json)
            except (json.JSONDecodeError, ValueError):
                continue
            _extract_urls_from_dict(config, page_url, qualities)


def _extract_urls_from_dict(
    data: dict, page_url: str, qualities: List[dict], depth: int = 0
) -> None:
    """بازگشتی URL های ویدیو رو از dict استخراج میکنه."""
    if depth > 5:
        return

    for key, value in data.items():
        if isinstance(value, str) and (".mp4" in value or ".m3u8" in value):
            video_url = _normalize_url(value, page_url)
            if not video_url or not _is_video_cdn_url(video_url):
                continue
            if any(q["url"] == video_url for q in qualities):
                continue

            is_m3u8 = ".m3u8" in video_url
            url_res = re.search(r"(\d{3,4})p", video_url)

            if is_m3u8:
                label = (
                    f"📡 M3U8 {url_res.group(1)}p"
                    if url_res else "📡 M3U8 Stream"
                )
                method = "m3u8"
            else:
                label = (
                    f"🎥 MP4 {url_res.group(1)}p"
                    if url_res else f"🎥 MP4 ({key})"
                )
                method = "direct"

            qualities.append({"label": label, "url": video_url, "method": method})

        elif isinstance(value, dict):
            _extract_urls_from_dict(value, page_url, qualities, depth + 1)

        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _extract_urls_from_dict(item, page_url, qualities, depth + 1)
                elif isinstance(item, str) and (".mp4" in item or ".m3u8" in item):
                    video_url = _normalize_url(item, page_url)
                    if not video_url or not _is_video_cdn_url(video_url):
                        continue
                    if any(q["url"] == video_url for q in qualities):
                        continue
                    is_m3u8 = ".m3u8" in video_url
                    qualities.append({
                        "label": "📡 M3U8 Stream" if is_m3u8 else "🎥 MP4",
                        "url": video_url,
                        "method": "m3u8" if is_m3u8 else "direct",
                    })


async def _extract_m3u8_streams(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    """استخراج M3U8 stream ها از HTML."""
    found_m3u8: List[str] = []
    for m in re.finditer(r"""['"]([^'"]+\.m3u8[^'"]*)['"]""", html):
        m3u8_url = _normalize_url(m.group(1), page_url)
        if not m3u8_url or not _is_video_cdn_url(m3u8_url):
            continue
        if m3u8_url in found_m3u8:
            continue
        if any(q["url"] == m3u8_url for q in qualities):
            continue
        found_m3u8.append(m3u8_url)

    for m3u8_url in found_m3u8:
        sub_qualities = await _parse_m3u8_variants(m3u8_url)
        if sub_qualities:
            for sq in sub_qualities:
                if not any(q["url"] == sq["url"] for q in qualities):
                    qualities.append(sq)
        else:
            qualities.append({
                "label": "📡 M3U8 Stream",
                "url": m3u8_url,
                "method": "m3u8",
            })


async def _parse_m3u8_variants(master_url: str) -> List[dict]:
    """M3U8 master playlist رو پارس میکنه."""
    timeout = ClientTimeout(total=15, connect=8)
    content, status = await _fetch_with_retry(
        master_url, max_retries=2, timeout=timeout
    )
    if content is None:
        return []

    if "#EXT-X-STREAM-INF" not in content:
        return []

    base_url = master_url.rsplit("/", 1)[0] + "/"
    results = []
    lines = content.splitlines()

    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        if i + 1 >= len(lines):
            continue

        stream_uri = lines[i + 1].strip()
        if not stream_uri or stream_uri.startswith("#"):
            continue

        if not stream_uri.startswith("http"):
            stream_uri = base_url + stream_uri

        if not _is_video_cdn_url(stream_uri):
            logger.warning("Blocked M3U8 variant: %s", stream_uri)
            continue

        res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bw_m = re.search(r"BANDWIDTH=(\d+)", line)

        if res_m:
            height = int(res_m.group(2))
            label = f"📡 M3U8 {height}p"
        elif bw_m:
            bw_kb = int(bw_m.group(1)) // 1000
            label = f"📡 M3U8 ~{bw_kb}kbps"
        else:
            label = "📡 M3U8 Stream"

        results.append({"label": label, "url": stream_uri, "method": "m3u8"})

    return results


# ─── Download: Direct MP4 ──────────────────────────────────


async def download_xgroovy_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """
    دانلود لینک مستقیم MP4.

    Returns:
        (success, error_message, file_size)
    """
    if not _is_video_cdn_url(url):
        return False, "URL host not allowed", 0

    headers = {
        **_DEFAULT_HEADERS,
        "Referer": "https://www.xgroovy.com/",
        "Origin": "https://www.xgroovy.com",
    }

    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            success, error, size = await _do_direct_download(
                url, filepath, headers, progress_cb
            )
            if success:
                return True, "", size

            if error.startswith("HTTP 4") and "403" not in error:
                _cleanup_file(filepath)
                return False, error, 0

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            error = str(e)[:150]
            logger.warning(
                "Download attempt %d/%d failed: %s",
                attempt, MAX_RETRIES, error,
            )

        if attempt < MAX_RETRIES:
            _cleanup_file(filepath)
            await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)
    return False, f"Failed after {MAX_RETRIES} attempts: {error}", 0


async def _do_direct_download(
    url: str,
    filepath: str,
    headers: dict,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """اجرای واقعی دانلود مستقیم."""
    timeout = ClientTimeout(total=3600, connect=30, sock_read=120)

    async with _get_session(timeout) as session:
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            if resp.status != 200:
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
                        text = _format_progress(
                            downloaded, content_length, start_time, now
                        )
                        await progress_cb(text)

    size = os.path.getsize(filepath)
    return True, "", size


# ─── Download: M3U8 ────────────────────────────────────────


async def download_xgroovy_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """
    دانلود M3U8 stream با yt-dlp.

    Returns:
        (success, error_message, file_size)
    """
    if not _is_video_cdn_url(m3u8_url):
        return False, "URL host not allowed", 0

    if not shutil.which("yt-dlp"):
        logger.error("yt-dlp is not installed or not in PATH")
        return False, "yt-dlp is not installed", 0

    await progress_cb("📡 **دانلود M3U8 stream...**")

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--quiet",
            "--progress",
            "--newline",
            "-f", "best",
            "--hls-prefer-native",
            "--no-check-certificates",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", "Referer:https://www.xgroovy.com/",
            "--add-header", "Origin:https://www.xgroovy.com",
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "-o", filepath,
            m3u8_url,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        last_update = 0.0
        while True:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=120
                )
            except asyncio.TimeoutError:
                logger.warning("yt-dlp stdout read timed out, killing process")
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
                await progress_cb(f"📡 **Downloading M3U8...**\n`{text[:80]}`")

        await process.wait()

        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            logger.error(
                "yt-dlp failed (code %d): %s", process.returncode, stderr[:200]
            )
            _cleanup_file(filepath)
            return False, stderr[:200], 0

        filepath = _find_output_file(filepath)
        if not filepath:
            return False, "Output file not found", 0

        size = os.path.getsize(filepath)
        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(filepath)
            return False, "Downloaded file exceeds size limit", 0

        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.exception("M3U8 download error")
        _cleanup_file(filepath)
        return False, str(e)[:150], 0


def _find_output_file(filepath: str) -> Optional[str]:
    """پیدا کردن فایل خروجی yt-dlp."""
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
