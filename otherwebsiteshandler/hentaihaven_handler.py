"""
hentaihaven_handler.py
----------------------
استخراج لینک‌های دانلود از hentaihaven.xxx و ارسال ویدیو به کاربر.

روش کار:
  - سایت از iframe embed و multi-server استفاده میکنه
  - yt-dlp به عنوان روش اصلی (با impersonation برای Cloudflare)
  - curl_cffi/aiohttp برای پارس HTML و استخراج iframe/embed URLs
  - لینک‌های MP4 و M3U8 از صفحه embed استخراج میشن
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
from urllib.parse import urlparse, urljoin, unquote, parse_qs

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("HentaiHavenHandler")

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

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
SESSION_TTL = 30 * 60
MAX_RETRIES = 3
RETRY_DELAY = 2.0

_SITE_DOMAIN = "hentaihaven.xxx"
_SITE_URL = "https://hentaihaven.xxx"
_SITE_REFERER = f"{_SITE_URL}/"

_ALLOWED_HOSTS = frozenset({
    "hentaihaven.xxx",
    "www.hentaihaven.xxx",
})

_ALLOWED_HOST_SUFFIXES = (
    ".hentaihaven.xxx",
    # Streaming/CDN servers
    ".cdntrex.com",
    ".kvcdn.com",
    ".cdn13.com",
    ".betacdn.net",
    ".bcdn.cc",
    ".gvideo.io",
    ".googleapis.com",
    ".googleusercontent.com",
    ".phncdn.com",
    ".mxdcontent.net",
    ".xvcdn.com",
    ".fastly.net",
    ".cloudfront.net",
    ".akamaized.net",
    ".hwcdn.net",
    # Common anime/hentai streaming CDNs
    ".streamtape.com",
    ".doodstream.com",
    ".vidstreaming.io",
    ".mp4upload.com",
    ".sendvid.com",
    ".fembed.com",
    ".mixdrop.co",
    ".upstream.to",
    ".streamsb.net",
    ".vidoza.net",
    ".filemoon.sx",
    ".voe.sx",
)

_IMPERSONATE_TARGETS = [
    "chrome",
    "chrome:120",
    "chrome:110",
    "edge",
    "safari",
]

hentaihaven_sessions: Dict[str, dict] = {}
ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def _is_allowed_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return (
            host in _ALLOWED_HOSTS
            or any(host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES)
        )
    except Exception:
        return False


def _is_video_cdn_url(url: str) -> bool:
    if _is_allowed_host(url):
        return True
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if parsed.scheme in ("http", "https") and (
            path.endswith(".mp4")
            or path.endswith(".m3u8")
            or path.endswith(".webm")
            or path.endswith(".mkv")
            or path.endswith(".ts")
            or "/video/" in path
            or "/media/" in path
            or "/hls/" in path
            or "/get_file/" in path
            or "/videos/" in path
            or "/embed/" in path
            or "/stream/" in path
            or "/e/" in path
        ):
            return True
    except Exception:
        pass
    return False


def is_hentaihaven_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به hentaihaven هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(f".{_SITE_DOMAIN}")
    except Exception:
        return False


def cleanup_expired_sessions() -> int:
    now = time.time()
    expired = [
        sid for sid, data in hentaihaven_sessions.items()
        if now - data.get("created_at", 0) > SESSION_TTL
    ]
    for sid in expired:
        hentaihaven_sessions.pop(sid, None)
    if expired:
        logger.info("Cleaned up %d expired hentaihaven sessions", len(expired))
    return len(expired)


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Cleaned up file: %s", filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _normalize_url(url: str, base_url: str = "") -> Optional[str]:
    url = url.replace("\\/", "/").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http") and base_url:
        url = urljoin(base_url, url)
    elif not url.startswith("http"):
        return None
    return url


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
        f"📥 **Downloading...**\n"
        f"💾 {dl_mb:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
    )


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


# ─── HTTP helpers ───────────────────────────────────────────


@asynccontextmanager
async def _get_session(timeout: Optional[ClientTimeout] = None):
    t = timeout or ClientTimeout(total=30, connect=10)
    jar = aiohttp.CookieJar(unsafe=True)
    session = aiohttp.ClientSession(
        timeout=t, headers=_DEFAULT_HEADERS, cookie_jar=jar
    )
    try:
        yield session
    finally:
        await session.close()


async def _fetch_with_cookies(
    url: str,
    referer: Optional[str] = None,
    max_retries: int = MAX_RETRIES,
    timeout: Optional[ClientTimeout] = None,
) -> Tuple[Optional[str], int, Optional[str]]:
    """
    دریافت محتوای URL با cookie jar.

    Returns:
        (content, status_code, final_url)
    """
    last_error = ""
    t = timeout or ClientTimeout(total=30, connect=10)
    ref = referer or _SITE_REFERER

    for attempt in range(1, max_retries + 1):
        try:
            jar = aiohttp.CookieJar(unsafe=True)
            async with aiohttp.ClientSession(
                timeout=t, headers=_DEFAULT_HEADERS, cookie_jar=jar
            ) as session:
                # صفحه اصلی برای کوکی
                try:
                    async with session.get(
                        _SITE_REFERER, allow_redirects=True,
                    ) as home_resp:
                        await home_resp.read()
                except Exception:
                    pass

                merged = {**_DEFAULT_HEADERS, "Referer": ref}
                async with session.get(
                    url, headers=merged, allow_redirects=True
                ) as resp:
                    final_url = str(resp.url)
                    if resp.status == 200:
                        content = await resp.text(errors="replace")
                        return content, 200, final_url
                    last_error = f"HTTP {resp.status}"
                    if 400 <= resp.status < 500 and resp.status != 403:
                        return None, resp.status, final_url

        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = str(e)[:120]

        if attempt < max_retries:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None, 0, None


async def _fetch_with_curl_cffi(
    url: str,
    referer: Optional[str] = None,
) -> Tuple[Optional[str], int, Optional[str]]:
    """دریافت صفحه با curl_cffi."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None, 0, None

    ref = referer or _SITE_REFERER
    try:
        async with AsyncSession() as session:
            try:
                await session.get(
                    _SITE_REFERER, impersonate="chrome", timeout=15,
                )
            except Exception:
                pass

            resp = await session.get(
                url,
                impersonate="chrome",
                headers={"Referer": ref, "Accept-Language": "en-US,en;q=0.9"},
                allow_redirects=True,
                timeout=30,
            )
            final_url = str(resp.url) if hasattr(resp, "url") else url
            if resp.status_code == 200:
                return resp.text, 200, final_url
            return None, resp.status_code, final_url

    except Exception as e:
        logger.warning("curl_cffi fetch failed: %s", e)
        return None, 0, None


async def _fetch_with_retry(
    url: str,
    headers: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
    timeout: Optional[ClientTimeout] = None,
) -> Tuple[Optional[str], int]:
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
        if attempt < max_retries:
            await asyncio.sleep(RETRY_DELAY * attempt)
    return None, 0


# ─── yt-dlp extraction ─────────────────────────────────────


async def _extract_with_ytdlp(url: str) -> Tuple[List[dict], str]:
    """استخراج با yt-dlp."""
    if not shutil.which("yt-dlp"):
        return [], "yt-dlp not installed"

    has_imp = _check_impersonation_support()

    attempts = []
    # basic اول
    attempts.append(("basic", None))
    if has_imp:
        for target in _IMPERSONATE_TARGETS:
            attempts.append(("impersonate", target))
    attempts.append(("extractor-args", None))

    error = ""
    for method, target in attempts:
        qualities, title, error = await _try_ytdlp_extract(url, method, target)
        if qualities:
            return qualities, title
        if "Unsupported URL" in error:
            break

    return [], error if error else "Extraction failed"


async def _try_ytdlp_extract(
    url: str,
    method: str,
    target: Optional[str],
) -> Tuple[List[dict], str, str]:
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-download",
        "--dump-json",
        "--no-check-certificates",
        "--no-playlist",
    ]

    if method == "impersonate" and target:
        cmd.extend(["--impersonate", target])
        logger.info("Trying yt-dlp with --impersonate %s", target)
    elif method == "extractor-args":
        cmd.extend(["--extractor-args", "generic:impersonate=chrome"])
        logger.info("Trying yt-dlp with extractor-args")
    else:
        logger.info("Trying yt-dlp basic")

    cmd.append(url)

    try:
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
            return [], "", "yt-dlp timed out"

        if process.returncode != 0:
            err = stderr.decode(errors="replace")[:300]
            logger.debug("yt-dlp failed (%s/%s): %s", method, target, err)
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
            qualities.append({
                "label": label,
                "url": direct_url,
                "method": "m3u8" if is_m3u8 else "direct",
            })
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
            protocol in ("m3u8", "m3u8_native")
            or ".m3u8" in fmt_url
            or ext == "m3u8"
        )

        size_str = f" ({filesize / 1024 / 1024:.0f}MB)" if filesize else ""
        if height:
            label = (
                f"📡 M3U8 {height}p{size_str}"
                if is_m3u8
                else f"🎥 {ext.upper()} {height}p{size_str}"
            )
        elif format_note:
            label = f"🎥 {format_note}{size_str}"
        else:
            label = f"🎥 {ext.upper()}{size_str}"

        qualities.append({
            "label": label,
            "url": fmt_url,
            "method": "m3u8" if is_m3u8 else "direct",
        })

    return qualities


# ─── HTML parsing ───────────────────────────────────────────


def _extract_title(html: str) -> str:
    m = re.search(
        r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:title["\']',
            html, re.IGNORECASE,
        )
    if m:
        return m.group(1).strip()

    m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*[Hh]entai\s*[Hh]aven.*$", "", title).strip()
        return title or "Untitled"

    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return "Untitled"


async def _extract_from_iframes(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    """
    استخراج iframe/embed URLs و سعی در گرفتن لینک ویدیو از اونا.
    HentaiHaven معمولاً ویدیو رو از iframe لود میکنه.
    """
    iframe_urls = []

    # iframe src
    for m in re.finditer(
        r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE
    ):
        iframe_url = _normalize_url(m.group(1), page_url)
        if iframe_url and iframe_url not in iframe_urls:
            iframe_urls.append(iframe_url)

    # data-src (lazy load)
    for m in re.finditer(
        r'<iframe[^>]+data-src=["\']([^"\']+)["\']', html, re.IGNORECASE
    ):
        iframe_url = _normalize_url(m.group(1), page_url)
        if iframe_url and iframe_url not in iframe_urls:
            iframe_urls.append(iframe_url)

    # embed tags
    for m in re.finditer(
        r'<embed[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE
    ):
        embed_url = _normalize_url(m.group(1), page_url)
        if embed_url and embed_url not in iframe_urls:
            iframe_urls.append(embed_url)

    # JS embed URLs
    for m in re.finditer(
        r"""(?:embedUrl|embed_url|iframe_src|player_url|src)\s*[:=]\s*['"]([^'"]+)['"]""",
        html,
    ):
        embed_url = _normalize_url(m.group(1), page_url)
        if embed_url and embed_url not in iframe_urls:
            # فیلتر URL های نامربوط
            parsed = urlparse(embed_url)
            if parsed.path and len(parsed.path) > 3:
                iframe_urls.append(embed_url)

    logger.debug("Found %d iframe/embed URLs", len(iframe_urls))

    # هر iframe رو بررسی کن
    for iframe_url in iframe_urls[:5]:  # حداکثر 5 تا
        logger.debug("Checking iframe: %s", iframe_url[:80])

        # اول با yt-dlp سعی کن
        iframe_qualities = await _extract_iframe_with_ytdlp(iframe_url)
        if iframe_qualities:
            for q in iframe_qualities:
                if not any(eq["url"] == q["url"] for eq in qualities):
                    qualities.append(q)
            continue

        # بعد HTML iframe رو بگیر و پارس کن
        await _extract_iframe_html(iframe_url, page_url, qualities)


async def _extract_iframe_with_ytdlp(url: str) -> List[dict]:
    """استخراج لینک ویدیو از iframe URL با yt-dlp."""
    if not shutil.which("yt-dlp"):
        return []

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
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=30
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return []

        if process.returncode != 0:
            return []

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return []

        data = json.loads(raw.split("\n")[0])
        return _parse_ytdlp_formats(data)

    except Exception:
        return []


async def _extract_iframe_html(
    iframe_url: str, page_url: str, qualities: List[dict]
) -> None:
    """HTML iframe رو بگیر و لینک‌های ویدیو رو استخراج کن."""
    # اول با curl_cffi
    html = None
    if _check_impersonation_support():
        html, status, _ = await _fetch_with_curl_cffi(iframe_url, referer=page_url)

    # بعد با aiohttp
    if not html:
        html, status, _ = await _fetch_with_cookies(
            iframe_url, referer=page_url, max_retries=1
        )

    if not html:
        return

    # استخراج لینک‌های ویدیو از HTML iframe
    _extract_from_video_tag(html, iframe_url, qualities)
    _extract_from_source_tags(html, iframe_url, qualities)
    _extract_from_js_vars(html, iframe_url, qualities)
    _extract_all_mp4_links(html, iframe_url, qualities)
    _extract_all_m3u8_links(html, iframe_url, qualities)
    _extract_from_player_setup(html, iframe_url, qualities)


def _extract_from_video_tag(
    html: str, page_url: str, qualities: List[dict]
) -> None:
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
            qualities.append({"label": f"📡 M3U8 {q_label}", "url": src, "method": "m3u8"})
        else:
            qualities.append({"label": f"🎥 MP4 {q_label}", "url": src, "method": "direct"})


def _extract_from_js_vars(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    js_patterns = [
        (r"""(?:var\s+)?video_url\s*[:=]\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""(?:var\s+)?video[_]?[Ff]ile\s*[:=]\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""(?:var\s+)?video(?:Url|_src|Src|_url)\s*[:=]\s*['"]([^'"]+)['"]""", None),
        (r"""file\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""['"]?(\d{3,4})['"]?\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", "numbered"),
        (r"""video_url\s*[:=]\s*decodeURIComponent\s*\(\s*['"]([^'"]+)['"]""", "encoded"),
        # الگوهای خاص پلیرهای انیمه
        (r"""sources\s*:\s*\[\s*\{\s*file\s*:\s*['"]([^'"]+)['"]""", None),
        (r"""source\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""src\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
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
            qualities.append({"label": label, "url": video_url, "method": "direct"})


def _extract_from_player_setup(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    """
    استخراج از JWPlayer/Plyr/VideoJS setup.
    الگوهای رایج:
      jwplayer().setup({sources: [{file: "..."}]})
      player.source = {sources: [{src: "..."}]}
      videojs().src({src: "..."})
    """
    # JWPlayer sources
    for m in re.finditer(
        r"sources\s*:\s*(\[.+?\])", html, re.DOTALL
    ):
        raw = m.group(1)
        raw = re.sub(r"'", '"', raw)
        # حذف trailing comma
        raw = re.sub(r",\s*]", "]", raw)
        raw = re.sub(r",\s*}", "}", raw)
        try:
            sources = json.loads(raw)
            for src in sources:
                if not isinstance(src, dict):
                    continue
                file_url = src.get("file") or src.get("src") or ""
                if not file_url:
                    continue
                video_url = _normalize_url(file_url, page_url)
                if not video_url or not _is_video_cdn_url(video_url):
                    continue
                if any(q["url"] == video_url for q in qualities):
                    continue

                is_m3u8 = ".m3u8" in video_url
                label_val = src.get("label", "")
                if label_val:
                    prefix = "📡 M3U8" if is_m3u8 else "🎥 MP4"
                    label = f"{prefix} {label_val}"
                else:
                    url_res = re.search(r"(\d{3,4})p", video_url)
                    if url_res:
                        prefix = "📡 M3U8" if is_m3u8 else "🎥 MP4"
                        label = f"{prefix} {url_res.group(1)}p"
                    else:
                        label = "📡 M3U8 Stream" if is_m3u8 else "🎥 MP4"

                qualities.append({
                    "label": label,
                    "url": video_url,
                    "method": "m3u8" if is_m3u8 else "direct",
                })
        except (json.JSONDecodeError, ValueError):
            pass

    # Plyr/VideoJS source
    for m in re.finditer(
        r"""(?:source|src)\s*[:=]\s*['"]([^'"]+(?:\.mp4|\.m3u8)[^'"]*)['"]""",
        html,
    ):
        video_url = _normalize_url(m.group(1), page_url)
        if not video_url or not _is_video_cdn_url(video_url):
            continue
        if any(q["url"] == video_url for q in qualities):
            continue
        is_m3u8 = ".m3u8" in video_url
        url_res = re.search(r"(\d{3,4})p", video_url)
        if url_res:
            label = f"📡 M3U8 {url_res.group(1)}p" if is_m3u8 else f"🎥 MP4 {url_res.group(1)}p"
        else:
            label = "📡 M3U8 Stream" if is_m3u8 else "🎥 MP4"
        qualities.append({
            "label": label, "url": video_url,
            "method": "m3u8" if is_m3u8 else "direct",
        })


def _extract_all_mp4_links(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    for m in re.finditer(r"""['"]([^'"]*\.mp4(?:\?[^'"]*)?)['"]\s*""", html):
        raw_url = m.group(1)
        video_url = _normalize_url(raw_url, page_url)
        if not video_url or not video_url.startswith("http"):
            continue
        parsed = urlparse(video_url)
        if len(parsed.path) < 5:
            continue
        if any(q["url"] == video_url for q in qualities):
            continue
        url_res = re.search(r"(\d{3,4})p", video_url)
        label = f"🎥 MP4 {url_res.group(1)}p" if url_res else "🎥 MP4"
        qualities.append({"label": label, "url": video_url, "method": "direct"})


def _extract_all_m3u8_links(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    for m in re.finditer(r"""['"]([^'"]*\.m3u8(?:\?[^'"]*)?)['"]\s*""", html):
        raw_url = m.group(1)
        m3u8_url = _normalize_url(raw_url, page_url)
        if not m3u8_url or not m3u8_url.startswith("http"):
            continue
        if any(q["url"] == m3u8_url for q in qualities):
            continue
        url_res = re.search(r"(\d{3,4})p", m3u8_url)
        label = f"📡 M3U8 {url_res.group(1)}p" if url_res else "📡 M3U8 Stream"
        qualities.append({"label": label, "url": m3u8_url, "method": "m3u8"})


def _extract_server_tabs(html: str) -> List[dict]:
    """
    استخراج لیست سرورها از تب‌های صفحه.
    HentaiHaven معمولاً چند سرور داره.
    """
    servers = []

    # الگوی data-src یا data-url در تب‌ها
    for m in re.finditer(
        r'(?:data-src|data-url|data-video|data-embed)\s*=\s*["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ):
        url = m.group(1).strip()
        if url and url not in [s["url"] for s in servers]:
            servers.append({"url": url, "name": f"Server {len(servers) + 1}"})

    # الگوی onclick با URL
    for m in re.finditer(
        r"""onclick\s*=\s*["'][^"']*(?:load|change|switch)[^"']*\(\s*['"]([^'"]+)['"]""",
        html, re.IGNORECASE,
    ):
        url = m.group(1).strip()
        if url.startswith("http") and url not in [s["url"] for s in servers]:
            servers.append({"url": url, "name": f"Server {len(servers) + 1}"})

    return servers


# ─── Main extraction ───────────────────────────────────────


async def extract_hentaihaven_qualities(url: str) -> Tuple[List[dict], str]:
    """
    لینک‌های کیفیت مختلف رو از صفحه hentaihaven استخراج میکنه.

    استراتژی:
      1. yt-dlp مستقیم روی URL اصلی
      2. HTML رو بگیر، iframe ها رو پیدا کن
      3. هر iframe رو با yt-dlp یا HTML parsing بررسی کن
    """
    if not is_hentaihaven_url(url):
        logger.warning("URL is not a valid hentaihaven URL: %s", url)
        return [], "Invalid URL"

    cleanup_expired_sessions()

    # ── روش 1: yt-dlp مستقیم ──
    logger.info("Attempting yt-dlp extraction for: %s", url)
    qualities, title = await _extract_with_ytdlp(url)
    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info("Extracted %d qualities (yt-dlp) for: %s", len(qualities), title[:60])
        return qualities, title

    # ── روش 2: HTML parsing + iframe extraction ──
    logger.info("yt-dlp failed, trying HTML parsing for: %s", url)

    html = None
    final_url = url

    # اول curl_cffi
    if _check_impersonation_support():
        html, status, final_url_result = await _fetch_with_curl_cffi(url)
        if final_url_result:
            final_url = final_url_result

    # بعد aiohttp
    if not html:
        html, status, final_url_result = await _fetch_with_cookies(url)
        if final_url_result:
            final_url = final_url_result

    if not html:
        logger.warning("Could not fetch page HTML for: %s", url)
        return [], "Could not fetch page"

    title = _extract_title(html)
    qualities = []

    # استخراج مستقیم از صفحه اصلی
    _extract_from_video_tag(html, final_url, qualities)
    _extract_from_source_tags(html, final_url, qualities)
    _extract_from_js_vars(html, final_url, qualities)
    _extract_from_player_setup(html, final_url, qualities)
    _extract_all_mp4_links(html, final_url, qualities)
    _extract_all_m3u8_links(html, final_url, qualities)

    # استخراج از iframe ها (مهم‌ترین بخش برای HentaiHaven)
    await _extract_from_iframes(html, final_url, qualities)

    # سرورهای مختلف
    servers = _extract_server_tabs(html)
    for server in servers[:3]:  # حداکثر 3 سرور
        server_url = _normalize_url(server["url"], final_url)
        if not server_url:
            continue
        logger.debug("Checking server: %s", server_url[:80])
        server_qualities = await _extract_iframe_with_ytdlp(server_url)
        for q in server_qualities:
            q["label"] = f"{q['label']} [{server['name']}]"
            if not any(eq["url"] == q["url"] for eq in qualities):
                qualities.append(q)

    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info("Extracted %d qualities (HTML) for: %s", len(qualities), title[:60])
        return qualities, title

    logger.warning("All extraction methods failed for: %s", url)

    has_ytdlp = shutil.which("yt-dlp") is not None
    has_curl_cffi = _check_impersonation_support()

    if not has_ytdlp:
        return [], "yt-dlp is not installed. Install: pip install yt-dlp"
    if not has_curl_cffi:
        return [], (
            "Protection detected. Install impersonation support:\n"
            "pip install curl_cffi\n"
            "or: pip install yt-dlp[default,curl-cffi]"
        )

    return [], "Extraction failed - site may have updated its protection"


# ─── Download helpers ───────────────────────────────────────


async def _download_with_curl_cffi(
    url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        await progress_cb("📥 **شروع دانلود (curl_cffi)...**")
        async with AsyncSession() as session:
            resp = await session.get(
                url, impersonate="chrome",
                headers={
                    "Referer": _SITE_REFERER,
                    "Origin": _SITE_URL,
                    "Accept": "*/*",
                },
                allow_redirects=True, timeout=600, stream=True,
            )
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}", 0

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > MAX_DOWNLOAD_SIZE:
                return False, f"File too large: {content_length / 1024 / 1024:.0f} MB", 0

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
                        await progress_cb(_format_progress(downloaded, content_length, start_time, now))

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


async def _download_with_ytdlp(
    url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    await progress_cb("📥 **شروع دانلود (yt-dlp)...**")
    try:
        cmd = [
            "yt-dlp", "--no-warnings", "--progress", "--newline",
            "--no-check-certificates", "-f", "best",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", f"Referer:{_SITE_REFERER}",
            "--add-header", f"Origin:{_SITE_URL}",
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "-o", filepath,
        ]
        if _check_impersonation_support():
            cmd.extend(["--impersonate", "chrome"])
        cmd.append(url)

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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


async def _download_with_aiohttp(
    url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    headers = {**_DEFAULT_HEADERS, "Referer": _SITE_REFERER, "Origin": _SITE_URL}
    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with _get_session(timeout) as session:
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if resp.status != 403 and 400 <= resp.status < 500:
                            _cleanup_file(filepath)
                            return False, error, 0
                    else:
                        content_length = int(resp.headers.get("Content-Length", 0))
                        if content_length > MAX_DOWNLOAD_SIZE:
                            return False, f"File too large: {content_length / 1024 / 1024:.0f} MB", 0
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
                                    await progress_cb(_format_progress(downloaded, content_length, start_time, now))
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
    return False, f"Failed after {MAX_RETRIES} attempts: {error}", 0


# ─── Download: Public API ──────────────────────────────────


async def download_hentaihaven_direct(
    url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود لینک مستقیم MP4."""
    if not _is_video_cdn_url(url):
        return False, "URL host not allowed", 0

    if _check_impersonation_support():
        logger.info("Trying download with curl_cffi: %s", url[:80])
        success, error, size = await _download_with_curl_cffi(url, filepath, progress_cb)
        if success:
            return True, "", size
        logger.info("curl_cffi download failed: %s", error)
        _cleanup_file(filepath)

    if shutil.which("yt-dlp"):
        logger.info("Trying download with yt-dlp: %s", url[:80])
        success, error, size = await _download_with_ytdlp(url, filepath, progress_cb)
        if success:
            return True, "", size
        logger.info("yt-dlp download failed: %s", error)
        _cleanup_file(filepath)

    logger.info("Trying download with aiohttp: %s", url[:80])
    success, error, size = await _download_with_aiohttp(url, filepath, progress_cb)
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0


async def download_hentaihaven_m3u8(
    m3u8_url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود M3U8 stream."""
    if not _is_video_cdn_url(m3u8_url):
        return False, "URL host not allowed", 0
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp is not installed", 0

    success, error, size = await _download_with_ytdlp(m3u8_url, filepath, progress_cb)
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error, 0
