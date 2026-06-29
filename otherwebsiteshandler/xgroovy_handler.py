"""
xgroovy_handler.py
------------------
استخراج لینک‌های دانلود از xgroovy.com و ارسال ویدیو به کاربر.

روش کار:
  - از yt-dlp با impersonation برای دور زدن Cloudflare استفاده میکنه
  - لینک‌های مستقیم MP4 و M3U8 از yt-dlp --dump-json استخراج میشن
  - اگه yt-dlp فشل شد، با curl_cffi مستقیم سعی میکنه
  - دانلود هم با curl_cffi/yt-dlp انجام میشه (نه aiohttp ساده)
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
from urllib.parse import urlparse, urljoin, unquote

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
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# حداکثر حجم دانلود: 2 گیگابایت
MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

# حداکثر عمر session (ثانیه): 30 دقیقه
SESSION_TTL = 30 * 60

MAX_RETRIES = 3
RETRY_DELAY = 2.0

_ALLOWED_HOSTS = frozenset(
    {
        "xgroovy.com",
        "www.xgroovy.com",
    }
)

_ALLOWED_HOST_SUFFIXES = (
    ".xgroovy.com",
    ".xgroovy-cdn.com",
    ".gvideo.io",
    ".cdntrex.com",
    ".googleapis.com",
    ".googleusercontent.com",
    ".cdn13.com",
    ".betacdn.net",
    ".bcdn.cc",
    ".phncdn.com",
    ".mxdcontent.net",
)

# لیست impersonate targets برای yt-dlp (به ترتیب اولویت)
_IMPERSONATE_TARGETS = [
    "chrome",
    "chrome:120",
    "chrome:110",
    "edge",
    "safari",
]

xgroovy_sessions: Dict[str, dict] = {}
ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def _is_allowed_host(url: str) -> bool:
    """بررسی اینکه URL به دامنه‌های مجاز اشاره میکنه."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or any(
            host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES
        )
    except Exception:
        return False


def _is_video_cdn_url(url: str) -> bool:
    """بررسی گسترده‌تر برای CDN های ویدیو."""
    if _is_allowed_host(url):
        return True
    try:
        parsed = urlparse(url)
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
        sid
        for sid, data in xgroovy_sessions.items()
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


def _check_impersonation_support() -> bool:
    """بررسی اینکه curl_cffi نصبه و yt-dlp از impersonation پشتیبانی میکنه."""
    try:
        import curl_cffi  # noqa: F401

        return True
    except ImportError:
        return False


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


# ─── HTTP helpers ───────────────────────────────────────────


@asynccontextmanager
async def _get_session(timeout: Optional[ClientTimeout] = None):
    """ساخت و مدیریت aiohttp session با cookie jar و cleanup خودکار."""
    t = timeout or ClientTimeout(total=30, connect=10)
    jar = aiohttp.CookieJar(unsafe=True)
    session = aiohttp.ClientSession(timeout=t, headers=_DEFAULT_HEADERS, cookie_jar=jar)
    try:
        yield session
    finally:
        await session.close()


async def _fetch_with_retry(
    url: str,
    headers: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
    timeout: Optional[ClientTimeout] = None,
) -> Tuple[Optional[str], int]:
    """دریافت ساده URL با retry (برای CDN ها و M3U8)."""
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
                attempt,
                max_retries,
                url,
                last_error,
            )
        if attempt < max_retries:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None, 0


# ─── yt-dlp extraction with impersonation ───────────────────


async def _extract_with_ytdlp(url: str) -> Tuple[List[dict], str]:
    """
    استخراج لینک‌های ویدیو با yt-dlp.
    اول با --impersonate سعی میکنه، اگه نشد بدون اون.
    """
    if not shutil.which("yt-dlp"):
        logger.error("yt-dlp is not installed")
        return [], "yt-dlp not installed"

    has_impersonation = _check_impersonation_support()

    attempts = []
    if has_impersonation:
        for target in _IMPERSONATE_TARGETS:
            attempts.append(("impersonate", target))
    attempts.append(("extractor-args", None))
    attempts.append(("basic", None))

    error = ""
    for method, target in attempts:
        qualities, title, error = await _try_ytdlp_extract(url, method, target)
        if qualities:
            return qualities, title
        if "HTTP Error 403" not in error and "Cloudflare" not in error:
            break

    return [], error if error else "Extraction failed"


async def _try_ytdlp_extract(
    url: str,
    method: str,
    target: Optional[str],
) -> Tuple[List[dict], str, str]:
    """یک تلاش استخراج با yt-dlp."""
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-download",
        "--dump-json",
        "--no-check-certificates",
    ]

    if method == "impersonate" and target:
        cmd.extend(["--impersonate", target])
        logger.info("Trying yt-dlp with --impersonate %s", target)
    elif method == "extractor-args":
        cmd.extend(["--extractor-args", "generic:impersonate=chrome"])
        logger.info("Trying yt-dlp with extractor-args impersonate")
    else:
        logger.info("Trying yt-dlp basic (no impersonation)")

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
            return [], "", "yt-dlp timed out"

        if process.returncode != 0:
            err = stderr.decode(errors="replace")[:300]
            logger.debug("yt-dlp attempt failed (%s/%s): %s", method, target, err)
            return [], "", err

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return [], "", "Empty output"

        first_line = raw.split("\n")[0]
        data = json.loads(first_line)

        title = data.get("title", "Untitled")
        qualities = _parse_ytdlp_formats(data)

        return qualities, title, ""

    except json.JSONDecodeError as e:
        return [], "", f"Invalid JSON: {e}"
    except Exception as e:
        return [], "", str(e)[:150]


def _parse_ytdlp_formats(data: dict) -> List[dict]:
    """پردازش فرمت‌های yt-dlp و تبدیل به لیست qualities."""
    qualities: List[dict] = []
    seen_urls = set()

    formats = data.get("formats", [])
    if not formats:
        direct_url = data.get("url", "")
        if direct_url:
            ext = data.get("ext", "mp4")
            height = data.get("height")
            label = f"🎥 {ext.upper()} {height}p" if height else f"🎥 {ext.upper()}"
            is_m3u8 = ext in ("m3u8", "m3u8_native") or ".m3u8" in direct_url
            qualities.append(
                {
                    "label": label,
                    "url": direct_url,
                    "method": "m3u8" if is_m3u8 else "direct",
                }
            )
        return qualities

    for fmt in formats:
        fmt_url = fmt.get("url", "")
        if not fmt_url or fmt_url in seen_urls:
            continue
        seen_urls.add(fmt_url)

        ext = fmt.get("ext", "mp4")
        height = fmt.get("height")
        vcodec = fmt.get("vcodec", "")
        protocol = fmt.get("protocol", "")
        format_note = fmt.get("format_note", "")
        filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0

        if vcodec == "none":
            continue

        is_m3u8 = (
            protocol in ("m3u8", "m3u8_native") or ".m3u8" in fmt_url or ext == "m3u8"
        )

        size_str = f" ({filesize / 1024 / 1024:.0f}MB)" if filesize else ""
        if height:
            if is_m3u8:
                label = f"📡 M3U8 {height}p{size_str}"
            else:
                label = f"🎥 {ext.upper()} {height}p{size_str}"
        elif format_note:
            label = f"🎥 {format_note}{size_str}"
        else:
            label = f"🎥 {ext.upper()}{size_str}"

        qualities.append(
            {
                "label": label,
                "url": fmt_url,
                "method": "m3u8" if is_m3u8 else "direct",
            }
        )

    return qualities


# ─── curl_cffi direct extraction ────────────────────────────


async def _extract_with_curl_cffi(url: str) -> Tuple[List[dict], str]:
    """استخراج با curl_cffi مستقیم (بدون yt-dlp)."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return [], "curl_cffi not installed"

    try:
        async with AsyncSession() as session:
            try:
                await session.get(
                    "https://www.xgroovy.com/",
                    impersonate="chrome",
                    timeout=15,
                )
            except Exception:
                pass

            resp = await session.get(
                url,
                impersonate="chrome",
                headers={
                    "Referer": "https://www.xgroovy.com/",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                allow_redirects=True,
                timeout=30,
            )

            if resp.status_code != 200:
                return [], f"HTTP {resp.status_code}"

            html = resp.text
            title = _extract_title(html)
            qualities: List[dict] = []

            _extract_from_video_tag(html, url, qualities)
            _extract_from_source_tags(html, url, qualities)
            _extract_from_json_ld(html, url, qualities)
            _extract_from_js_vars(html, url, qualities)
            _extract_from_player_config(html, url, qualities)

            return qualities, title

    except ImportError:
        return [], "curl_cffi not installed"
    except Exception as e:
        logger.warning("curl_cffi extraction failed: %s", e)
        return [], str(e)[:150]


# ─── Main extraction ───────────────────────────────────────


async def extract_xgroovy_qualities(url: str) -> Tuple[List[dict], str]:
    """
    لینک‌های کیفیت مختلف رو از صفحه xgroovy استخراج میکنه.

    استراتژی (به ترتیب):
      1. yt-dlp با --impersonate (بهترین روش برای Cloudflare)
      2. curl_cffi مستقیم (اگه yt-dlp impersonation نداشت)
      3. request ساده (بعید که کار کنه ولی سعی میکنه)
    """
    if not is_xgroovy_url(url):
        logger.warning("URL is not a valid xgroovy URL: %s", url)
        return [], "Invalid URL"

    cleanup_expired_sessions()

    # ── روش 1: yt-dlp (اصلی‌ترین روش) ──
    logger.info("Attempting extraction with yt-dlp for: %s", url)
    qualities, title = await _extract_with_ytdlp(url)
    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info(
            "Extracted %d qualities (yt-dlp) for: %s",
            len(qualities),
            title[:60],
        )
        return qualities, title

    # ── روش 2: curl_cffi مستقیم ──
    logger.info("yt-dlp failed, trying curl_cffi for: %s", url)
    qualities, title = await _extract_with_curl_cffi(url)
    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info(
            "Extracted %d qualities (curl_cffi) for: %s",
            len(qualities),
            title[:60],
        )
        return qualities, title

    # ── روش 3: request ساده (آخرین تلاش) ──
    logger.info("curl_cffi failed, trying direct request for: %s", url)
    html, status = await _fetch_with_retry(url)
    if html:
        title = _extract_title(html)
        qualities = []
        _extract_from_video_tag(html, url, qualities)
        _extract_from_source_tags(html, url, qualities)
        _extract_from_js_vars(html, url, qualities)
        if qualities:
            qualities.sort(key=_quality_sort_key, reverse=True)
            return qualities, title

    logger.warning("All extraction methods failed for: %s", url)

    has_ytdlp = shutil.which("yt-dlp") is not None
    has_curl_cffi = _check_impersonation_support()

    if not has_ytdlp:
        return [], "yt-dlp is not installed. Install: pip install yt-dlp"
    if not has_curl_cffi:
        return [], (
            "Cloudflare protection detected. Install impersonation support:\n"
            "pip install curl_cffi\n"
            "or: pip install yt-dlp[default,curl-cffi]"
        )

    return [], "Extraction failed - site may have updated its protection"


# ─── HTML extraction helpers ───────────────────────────────


def _extract_title(html: str) -> str:
    """استخراج عنوان ویدیو از HTML."""
    m = re.search(
        r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:title["\']',
            html,
            re.IGNORECASE,
        )
    if m:
        return m.group(1).strip()

    m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*[Xx][Gg]roovy.*$", "", title).strip()
        return title or "Untitled"

    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return "Untitled"


def _extract_from_video_tag(html: str, page_url: str, qualities: List[dict]) -> None:
    """استخراج از تگ <video> با attribute src."""
    for m in re.finditer(r'<video[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
        video_url = _normalize_url(m.group(1), page_url)
        if not video_url or not _is_video_cdn_url(video_url):
            continue
        if any(q["url"] == video_url for q in qualities):
            continue
        qualities.append(
            {
                "label": "🎥 MP4 (video tag)",
                "url": video_url,
                "method": "direct",
            }
        )


def _extract_from_source_tags(html: str, page_url: str, qualities: List[dict]) -> None:
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
            qualities.append(
                {
                    "label": f"📡 M3U8 {q_label}",
                    "url": src,
                    "method": "m3u8",
                }
            )
        else:
            qualities.append(
                {
                    "label": f"🎥 MP4 {q_label}",
                    "url": src,
                    "method": "direct",
                }
            )


def _extract_from_json_ld(html: str, page_url: str, qualities: List[dict]) -> None:
    """استخراج از JSON-LD structured data."""
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
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
            qualities.append(
                {
                    "label": "🎥 MP4 (structured data)",
                    "url": video_url,
                    "method": "direct",
                }
            )


def _extract_from_js_vars(html: str, page_url: str, qualities: List[dict]) -> None:
    """استخراج لینک ویدیو از متغیرهای JavaScript."""
    js_patterns = [
        (r"""(?:var\s+)?video_url\s*[:=]\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""(?:var\s+)?video[_]?[Ff]ile\s*[:=]\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""(?:var\s+)?video(?:Url|_src|Src|_url)\s*[:=]\s*['"]([^'"]+)['"]""", None),
        (r"""file\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""src\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""['"]?(\d{3,4})['"]?\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", "numbered"),
        (
            r"""video_url\s*[:=]\s*decodeURIComponent\s*\(\s*['"]([^'"]+)['"]""",
            "encoded",
        ),
    ]

    for pattern, ptype in js_patterns:
        for m in re.finditer(pattern, html):
            if ptype == "numbered":
                quality_num = m.group(1)
                raw_url = m.group(2)
                label = f"🎥 MP4 {quality_num}p"
            elif ptype == "encoded":
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
            qualities.append(
                {
                    "label": label,
                    "url": video_url,
                    "method": "direct",
                }
            )


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
            raw_json = re.sub(r"'", '"', m.group(1))
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
                label = f"📡 M3U8 {url_res.group(1)}p" if url_res else "📡 M3U8 Stream"
            else:
                label = f"🎥 MP4 {url_res.group(1)}p" if url_res else f"🎥 MP4 ({key})"
            qualities.append(
                {
                    "label": label,
                    "url": video_url,
                    "method": "m3u8" if is_m3u8 else "direct",
                }
            )
        elif isinstance(value, dict):
            _extract_urls_from_dict(value, page_url, qualities, depth + 1)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _extract_urls_from_dict(item, page_url, qualities, depth + 1)


# ─── Download: curl_cffi ───────────────────────────────────


async def _download_with_curl_cffi(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود با curl_cffi و TLS fingerprint واقعی Chrome."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        await progress_cb("📥 **شروع دانلود (curl_cffi)...**")

        async with AsyncSession() as session:
            resp = await session.get(
                url,
                impersonate="chrome",
                headers={
                    "Referer": "https://www.xgroovy.com/",
                    "Origin": "https://www.xgroovy.com",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
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

    except Exception as e:
        logger.warning("curl_cffi download error: %s", e)
        return False, str(e)[:150], 0


# ─── Download: yt-dlp ──────────────────────────────────────


async def _download_with_ytdlp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود با yt-dlp (هم direct هم m3u8 رو هندل میکنه)."""
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
            "-f",
            "best",
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
            "Referer:https://www.xgroovy.com/",
            "--add-header",
            "Origin:https://www.xgroovy.com",
            "--add-header",
            f"User-Agent:{_USER_AGENT}",
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

        cmd.append(url)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        last_update = 0.0
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=120)
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
        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(actual_path)
            return False, "File exceeds size limit", 0

        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        return False, str(e)[:150], 0


# ─── Download: aiohttp fallback ────────────────────────────


async def _download_with_aiohttp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود با aiohttp ساده (آخرین تلاش)."""
    headers = {
        **_DEFAULT_HEADERS,
        "Referer": "https://www.xgroovy.com/",
        "Origin": "https://www.xgroovy.com",
    }

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
                            _cleanup_file(filepath)
                            return False, error, 0
                    else:
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
                                    return (
                                        False,
                                        "Download exceeded size limit",
                                        0,
                                    )

                                now = time.time()
                                if now - last_update >= 2.0:
                                    last_update = now
                                    await progress_cb(
                                        _format_progress(
                                            downloaded,
                                            content_length,
                                            start_time,
                                            now,
                                        )
                                    )

                        size = os.path.getsize(filepath)
                        return True, "", size

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            error = str(e)[:150]

        if attempt < MAX_RETRIES:
            _cleanup_file(filepath)
            await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)
    return False, f"Failed after {MAX_RETRIES} attempts: {error}", 0


# ─── Download: Public API ──────────────────────────────────


async def download_xgroovy_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """
    دانلود لینک مستقیم MP4.
    اول با curl_cffi، بعد yt-dlp، آخر aiohttp.

    Returns:
        (success, error_message, file_size)
    """
    if not _is_video_cdn_url(url):
        return False, "URL host not allowed", 0

    # ── روش 1: curl_cffi ──
    if _check_impersonation_support():
        logger.info("Trying download with curl_cffi: %s", url[:80])
        success, error, size = await _download_with_curl_cffi(
            url, filepath, progress_cb
        )
        if success:
            return True, "", size
        logger.info("curl_cffi download failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 2: yt-dlp ──
    if shutil.which("yt-dlp"):
        logger.info("Trying download with yt-dlp: %s", url[:80])
        success, error, size = await _download_with_ytdlp(url, filepath, progress_cb)
        if success:
            return True, "", size
        logger.info("yt-dlp download failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 3: aiohttp ──
    logger.info("Trying download with aiohttp: %s", url[:80])
    success, error, size = await _download_with_aiohttp(url, filepath, progress_cb)
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0


async def download_xgroovy_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """
    دانلود M3U8 stream.
    از yt-dlp استفاده میکنه چون بهترین ابزار برای M3U8 هست.

    Returns:
        (success, error_message, file_size)
    """
    if not _is_video_cdn_url(m3u8_url):
        return False, "URL host not allowed", 0

    if not shutil.which("yt-dlp"):
        return False, "yt-dlp is not installed", 0

    success, error, size = await _download_with_ytdlp(m3u8_url, filepath, progress_cb)
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0
