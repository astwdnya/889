"""
hentaihaven_handler.py
----------------------
استخراج لینک‌های دانلود از hentaihaven.xxx و ارسال ویدیو به کاربر.

روش کار:
  - سایت WordPress-based هست و از iframe embed استفاده میکنه
  - ویدیو از player.php?data=... لود میشه
  - yt-dlp روی iframe URL های واقعی اجرا میشه
  - curl_cffi/aiohttp برای پارس HTML
  - کاربر با دکمه کیفیت انتخاب میکنه
"""

import asyncio
import base64
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
    ".cdntrex.com",
    ".kvcdn.com",
    ".cdn13.com",
    ".betacdn.net",
    ".bcdn.cc",
    ".gvideo.io",
    ".googleapis.com",
    ".googleusercontent.com",
    ".fastly.net",
    ".cloudfront.net",
    ".akamaized.net",
    ".hwcdn.net",
    ".streamtape.com",
    ".stape.fun",
    ".doodstream.com",
    ".dood.to",
    ".dood.so",
    ".dood.watch",
    ".mp4upload.com",
    ".sendvid.com",
    ".fembed.com",
    ".mixdrop.co",
    ".mixdrop.to",
    ".upstream.to",
    ".streamsb.net",
    ".vidoza.net",
    ".filemoon.sx",
    ".filemoon.to",
    ".voe.sx",
    ".streamwish.to",
    ".streamwish.com",
    ".vidhide.com",
    ".vidguard.to",
    ".lulustream.com",
    ".emturbovid.com",
    ".turbovid.com",
)

# پسوندهای فایل‌هایی که نباید به عنوان ویدیو شناسایی بشن
_SKIP_EXTENSIONS = frozenset({
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map",
    ".json", ".xml", ".txt", ".html", ".htm", ".php",
})

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
        # فیلتر فایل‌های غیر ویدیویی
        for ext in _SKIP_EXTENSIONS:
            if path.endswith(ext):
                return False
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
            or "/stream/" in path
        ):
            return True
    except Exception:
        pass
    return False


def _is_embed_url(url: str) -> bool:
    """بررسی اینکه URL یه embed/player واقعی هست نه فایل JS/CSS."""
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        host = parsed.hostname or ""

        # فیلتر فایل‌های استاتیک
        for ext in _SKIP_EXTENSIONS:
            if path.endswith(ext) and "player" not in path:
                return False

        # فیلتر URL های WordPress غیر ویدیویی
        skip_paths = [
            "/wp-includes/",
            "/wp-content/themes/",
            "/wp-content/uploads/",
            "/wp-admin/",
            "/wp-json/",
            "/feed/",
            "jquery",
            "recaptcha",
            "google-analytics",
            "googletagmanager",
            "facebook",
            "twitter",
            "disqus",
        ]
        for skip in skip_paths:
            if skip in path.lower() or skip in host.lower():
                return False

        # URL های مثبت (embed/player)
        positive_patterns = [
            "/embed/",
            "/e/",
            "/player",
            "/watch/",
            "/video/",
            "/stream/",
            "player.php",
            "/play/",
        ]
        for pattern in positive_patterns:
            if pattern in path.lower():
                return True

        # هاست‌های شناخته شده streaming
        streaming_hosts = [
            "streamtape", "doodstream", "dood.", "mp4upload",
            "sendvid", "fembed", "mixdrop", "upstream",
            "streamsb", "vidoza", "filemoon", "voe.",
            "streamwish", "vidhide", "vidguard", "lulustream",
            "turbovid", "emturbovid",
        ]
        for sh in streaming_hosts:
            if sh in host:
                return True

        # اگه query parameter data داره (مثل player.php?data=...)
        if parsed.query and "data=" in parsed.query:
            return True

        return False

    except Exception:
        return False


def is_hentaihaven_url(url: str) -> bool:
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
    downloaded: int, content_length: int, start_time: float, now: float,
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
    return f"📥 **Downloading...**\n💾 {dl_mb:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"


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


def _is_valid_video_content(filepath: str) -> bool:
    """بررسی اینکه فایل دانلود شده واقعاً ویدیو هست نه JS/HTML."""
    try:
        if not os.path.exists(filepath):
            return False
        size = os.path.getsize(filepath)
        # فایل‌های کمتر از 100KB احتمالاً ویدیو نیستن
        if size < 100 * 1024:
            with open(filepath, "rb") as f:
                header = f.read(min(512, size))
            # بررسی magic bytes
            # MP4: ftyp
            if b"ftyp" in header[:12]:
                return True
            # WebM: 0x1A45DFA3
            if header[:4] == b"\x1a\x45\xdf\xa3":
                return True
            # MPEG-TS: 0x47
            if header[0:1] == b"\x47":
                return True
            # اگه متن/HTML/JS هست
            try:
                text = header.decode("utf-8", errors="ignore")
                if any(kw in text.lower() for kw in [
                    "<!doctype", "<html", "<script", "function(",
                    "jquery", "var ", "const ", "undefined",
                    "{", "/*", "//",
                ]):
                    logger.warning("Downloaded file is text/JS, not video")
                    return False
            except Exception:
                pass
        return True
    except Exception:
        return True


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


async def _fetch_page(
    url: str,
    referer: Optional[str] = None,
) -> Tuple[Optional[str], int, Optional[str]]:
    """
    دریافت صفحه - اول curl_cffi بعد aiohttp.

    Returns:
        (content, status_code, final_url)
    """
    ref = referer or _SITE_REFERER

    # curl_cffi اول (برای Cloudflare)
    if _check_impersonation_support():
        try:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession() as session:
                # کوکی از صفحه اصلی
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
                logger.debug("curl_cffi got status %d for %s", resp.status_code, url[:60])
        except ImportError:
            pass
        except Exception as e:
            logger.debug("curl_cffi failed: %s", e)

    # aiohttp fallback
    try:
        t = ClientTimeout(total=30, connect=10)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(
            timeout=t, headers=_DEFAULT_HEADERS, cookie_jar=jar
        ) as session:
            try:
                async with session.get(_SITE_REFERER, allow_redirects=True) as hr:
                    await hr.read()
            except Exception:
                pass

            async with session.get(
                url,
                headers={**_DEFAULT_HEADERS, "Referer": ref},
                allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                if resp.status == 200:
                    return await resp.text(errors="replace"), 200, final_url
                return None, resp.status, final_url
    except Exception as e:
        logger.debug("aiohttp failed: %s", e)
        return None, 0, None


# ─── yt-dlp helpers ─────────────────────────────────────────


async def _ytdlp_extract(url: str, use_impersonate: bool = True) -> Tuple[List[dict], str]:
    """استخراج با yt-dlp."""
    if not shutil.which("yt-dlp"):
        return [], "yt-dlp not installed"

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-download",
        "--dump-json",
        "--no-check-certificates",
        "--no-playlist",
    ]
    if use_impersonate and _check_impersonation_support():
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
                process.communicate(), timeout=45
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return [], "yt-dlp timed out"

        if process.returncode != 0:
            err = stderr.decode(errors="replace")[:200]
            logger.debug("yt-dlp failed for %s: %s", url[:60], err)
            return [], err

        raw = stdout.decode(errors="replace").strip()
        if not raw:
            return [], "Empty output"

        data = json.loads(raw.split("\n")[0])
        title = data.get("title", "Untitled")
        return _parse_ytdlp_formats(data), title

    except json.JSONDecodeError:
        return [], "Invalid JSON"
    except Exception as e:
        return [], str(e)[:150]


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
                "label": label, "url": direct_url,
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

        is_m3u8 = protocol in ("m3u8", "m3u8_native") or ".m3u8" in fmt_url or ext == "m3u8"
        size_str = f" ({filesize / 1024 / 1024:.0f}MB)" if filesize else ""

        if height:
            label = f"📡 M3U8 {height}p{size_str}" if is_m3u8 else f"🎥 {ext.upper()} {height}p{size_str}"
        elif format_note:
            label = f"🎥 {format_note}{size_str}"
        else:
            label = f"🎥 {ext.upper()}{size_str}"

        qualities.append({
            "label": label, "url": fmt_url,
            "method": "m3u8" if is_m3u8 else "direct",
        })

    return qualities


# ─── Iframe / embed extraction ──────────────────────────────


def _find_video_iframes(html: str, page_url: str) -> List[str]:
    """
    پیدا کردن iframe/embed URL های واقعی ویدیو.
    فیلتر سنگین برای حذف JS/CSS/تبلیغات.
    """
    candidates = []

    # iframe src و data-src
    for m in re.finditer(
        r'<iframe[^>]+(?:src|data-src)\s*=\s*["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ):
        url = _normalize_url(m.group(1), page_url)
        if url:
            candidates.append(url)

    # embed src
    for m in re.finditer(
        r'<embed[^>]+src\s*=\s*["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ):
        url = _normalize_url(m.group(1), page_url)
        if url:
            candidates.append(url)

    # player.php?data=... (خاص HentaiHaven)
    for m in re.finditer(
        r'["\']([^"\']*player\.php\?[^"\']+)["\']',
        html,
    ):
        url = _normalize_url(m.group(1), page_url)
        if url:
            candidates.append(url)

    # data-video, data-embed, data-src attributes روی هر تگ
    for m in re.finditer(
        r'(?:data-video|data-embed|data-player|data-url)\s*=\s*["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ):
        url = _normalize_url(m.group(1), page_url)
        if url:
            candidates.append(url)

    # JS: loadPlayer("url") / changeServer("url") / setSource("url")
    for m in re.finditer(
        r"""(?:loadPlayer|changeServer|setSource|loadVideo|playVideo)\s*\(\s*['"]([^'"]+)['"]""",
        html,
    ):
        url = _normalize_url(m.group(1), page_url)
        if url:
            candidates.append(url)

    # فیلتر: فقط URL های embed/player واقعی
    filtered = []
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        if _is_embed_url(url):
            filtered.append(url)
            logger.debug("Valid embed URL: %s", url[:80])
        else:
            logger.debug("Skipped non-embed URL: %s", url[:60])

    return filtered


async def _extract_from_embed(
    embed_url: str, page_url: str, server_name: str = "",
) -> List[dict]:
    """
    استخراج لینک ویدیو از یه embed URL.
    اول yt-dlp، بعد HTML parsing.
    """
    qualities = []
    label_prefix = f"[{server_name}] " if server_name else ""

    # ── yt-dlp روی embed URL ──
    ytdlp_qualities, _ = await _ytdlp_extract(embed_url)
    if ytdlp_qualities:
        for q in ytdlp_qualities:
            q["label"] = f"{label_prefix}{q['label']}"
            qualities.append(q)
        return qualities

    # ── HTML parsing embed page ──
    html, status, final_url = await _fetch_page(embed_url, referer=page_url)
    if not html:
        return []

    _extract_video_from_html(html, final_url or embed_url, qualities)

    # label prefix اضافه کن
    for q in qualities:
        q["label"] = f"{label_prefix}{q['label']}"

    return qualities


def _extract_video_from_html(
    html: str, page_url: str, qualities: List[dict]
) -> None:
    """استخراج تمام لینک‌های ویدیو از HTML."""
    # video tag
    for m in re.finditer(
        r'<video[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE
    ):
        _add_quality(m.group(1), page_url, qualities)

    # source tags
    for m in re.finditer(
        r'<source[^>]+src=["\']([^"\']+)["\']([^>]*)', html, re.IGNORECASE
    ):
        src = m.group(1)
        attrs = m.group(2)
        label_m = re.search(r'label=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        extra_label = label_m.group(1) if label_m else None
        _add_quality(src, page_url, qualities, extra_label=extra_label)

    # JWPlayer / player sources JSON
    for m in re.finditer(r"sources\s*:\s*(\[.+?\])", html, re.DOTALL):
        raw = re.sub(r"'", '"', m.group(1))
        raw = re.sub(r",\s*]", "]", raw)
        raw = re.sub(r",\s*}", "}", raw)
        try:
            sources = json.loads(raw)
            for src in sources:
                if isinstance(src, dict):
                    file_url = src.get("file") or src.get("src") or ""
                    label_val = src.get("label", "")
                    if file_url:
                        _add_quality(file_url, page_url, qualities, extra_label=label_val)
        except (json.JSONDecodeError, ValueError):
            pass

    # JS vars
    js_patterns = [
        r"""(?:var\s+)?(?:video_url|videoUrl|file|source|src)\s*[:=]\s*['"]([^'"]+\.(?:mp4|m3u8)[^'"]*)['"]""",
        r"""file\s*:\s*['"]([^'"]+\.(?:mp4|m3u8)[^'"]*)['"]""",
        r"""source\s*:\s*['"]([^'"]+\.(?:mp4|m3u8)[^'"]*)['"]""",
    ]
    for pattern in js_patterns:
        for m in re.finditer(pattern, html):
            _add_quality(m.group(1), page_url, qualities)

    # هر لینک mp4/m3u8
    for m in re.finditer(r"""['"]([^'"]*\.mp4(?:\?[^'"]*)?)['"]\s*""", html):
        _add_quality(m.group(1), page_url, qualities)
    for m in re.finditer(r"""['"]([^'"]*\.m3u8(?:\?[^'"]*)?)['"]\s*""", html):
        _add_quality(m.group(1), page_url, qualities)


def _add_quality(
    raw_url: str,
    page_url: str,
    qualities: List[dict],
    extra_label: Optional[str] = None,
) -> None:
    """اضافه کردن یه کیفیت به لیست با فیلتر و dedup."""
    video_url = _normalize_url(raw_url, page_url)
    if not video_url or not video_url.startswith("http"):
        return

    # فیلتر فایل‌های غیر ویدیویی
    parsed = urlparse(video_url)
    path = parsed.path.lower()
    for ext in _SKIP_EXTENSIONS:
        if path.endswith(ext):
            return
    if len(path) < 5:
        return

    if any(q["url"] == video_url for q in qualities):
        return

    is_m3u8 = ".m3u8" in video_url

    if extra_label:
        prefix = "📡 M3U8" if is_m3u8 else "🎥 MP4"
        label = f"{prefix} {extra_label}"
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


def _extract_server_list(html: str, page_url: str) -> List[dict]:
    """
    استخراج لیست سرورها از تب‌ها/دکمه‌ها.
    HentaiHaven معمولاً چند سرور داره.
    """
    servers = []
    seen = set()

    # الگوی تب سرور با data attribute
    for m in re.finditer(
        r'(?:data-src|data-url|data-video|data-embed|data-player)\s*=\s*["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ):
        url = _normalize_url(m.group(1), page_url)
        if url and url not in seen and _is_embed_url(url):
            seen.add(url)
            # سعی کن اسم سرور رو پیدا کن
            # معمولاً در تگ parent یا sibling هست
            servers.append({"url": url, "name": f"Server {len(servers) + 1}"})

    # الگوی onclick
    for m in re.finditer(
        r"""onclick\s*=\s*["'][^"']*['"]([^"']+(?:embed|player|stream|/e/)[^"']*)['"]""",
        html, re.IGNORECASE,
    ):
        url = _normalize_url(m.group(1), page_url)
        if url and url not in seen and _is_embed_url(url):
            seen.add(url)
            servers.append({"url": url, "name": f"Server {len(servers) + 1}"})

    return servers


# ─── Main extraction ───────────────────────────────────────


async def extract_hentaihaven_qualities(url: str) -> Tuple[List[dict], str]:
    """
    لینک‌های کیفیت مختلف رو از صفحه hentaihaven استخراج میکنه.

    استراتژی:
      1. صفحه اصلی رو بگیر (curl_cffi/aiohttp)
      2. iframe/embed URL های واقعی رو فیلتر کن
      3. هر embed رو با yt-dlp یا HTML parsing بررسی کن
    """
    if not is_hentaihaven_url(url):
        return [], "Invalid URL"

    cleanup_expired_sessions()

    # ── مرحله 1: HTML صفحه اصلی ──
    logger.info("Fetching page HTML for: %s", url)
    html, status, final_url = await _fetch_page(url)

    if not html:
        logger.warning("Could not fetch page (status=%s): %s", status, url)
        # آخرین تلاش: yt-dlp مستقیم
        qualities, title = await _ytdlp_extract(url)
        if qualities:
            qualities.sort(key=_quality_sort_key, reverse=True)
            return qualities, title
        return [], f"Could not fetch page (HTTP {status})"

    title = _extract_title(html)
    qualities: List[dict] = []
    page = final_url or url

    # ── مرحله 2: استخراج مستقیم از صفحه ──
    _extract_video_from_html(html, page, qualities)

    # ── مرحله 3: پیدا کردن iframe/embed های واقعی ──
    embed_urls = _find_video_iframes(html, page)
    logger.info("Found %d valid embed URLs", len(embed_urls))

    # سرورهای اضافی
    servers = _extract_server_list(html, page)
    for srv in servers:
        if srv["url"] not in embed_urls:
            embed_urls.append(srv["url"])

    # ── مرحله 4: بررسی هر embed ──
    for i, embed_url in enumerate(embed_urls[:8]):  # حداکثر 8
        logger.info("Processing embed %d/%d: %s", i + 1, len(embed_urls), embed_url[:80])
        server_name = f"Server {i + 1}" if len(embed_urls) > 1 else ""
        embed_qualities = await _extract_from_embed(embed_url, page, server_name)
        for q in embed_qualities:
            if not any(eq["url"] == q["url"] for eq in qualities):
                qualities.append(q)

    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info("Extracted %d qualities for: %s", len(qualities), title[:60])
        return qualities, title

    # ── آخرین تلاش: yt-dlp مستقیم روی URL اصلی ──
    logger.info("No qualities from embeds, trying yt-dlp on main URL")
    ytdlp_qualities, ytdlp_title = await _ytdlp_extract(url)
    if ytdlp_qualities:
        ytdlp_qualities.sort(key=_quality_sort_key, reverse=True)
        return ytdlp_qualities, ytdlp_title or title

    logger.warning("All extraction methods failed for: %s", url)

    if not shutil.which("yt-dlp"):
        return [], "yt-dlp is not installed. Install: pip install yt-dlp"
    if not _check_impersonation_support():
        return [], (
            "Protection detected. Install impersonation support:\n"
            "pip install curl_cffi"
        )

    return [], "Extraction failed - no video sources found in page"


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


# ─── Download helpers ───────────────────────────────────────


async def _download_with_curl_cffi(
    url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        await progress_cb("📥 **شروع دانلود...**")
        async with AsyncSession() as session:
            resp = await session.get(
                url, impersonate="chrome",
                headers={"Referer": _SITE_REFERER, "Accept": "*/*"},
                allow_redirects=True, timeout=600, stream=True,
            )
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}", 0

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > MAX_DOWNLOAD_SIZE:
                return False, f"File too large: {content_length / 1024 / 1024:.0f} MB", 0

            # بررسی content-type
            ct = resp.headers.get("Content-Type", "").lower()
            if any(t in ct for t in ["text/html", "text/javascript", "application/javascript"]):
                logger.warning("Content-Type is %s, not video", ct)
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
                        await progress_cb(_format_progress(downloaded, content_length, start_time, now))

        if not os.path.exists(filepath):
            return False, "File not created", 0

        # بررسی محتوای فایل
        if not _is_valid_video_content(filepath):
            _cleanup_file(filepath)
            return False, "Downloaded file is not a valid video", 0

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

        if not _is_valid_video_content(actual_path):
            _cleanup_file(actual_path)
            return False, "Downloaded file is not a valid video", 0

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
    headers = {**_DEFAULT_HEADERS, "Referer": _SITE_REFERER}
    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with _get_session(timeout) as session:
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if resp.status != 403 and 400 <= resp.status < 500:
                            return False, error, 0
                        continue

                    # بررسی content-type
                    ct = resp.headers.get("Content-Type", "").lower()
                    if any(t in ct for t in ["text/html", "text/javascript", "application/javascript"]):
                        return False, "Response is not a video file", 0

                    content_length = int(resp.headers.get("Content-Length", 0))
                    if content_length > MAX_DOWNLOAD_SIZE:
                        return False, f"File too large", 0

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

                    if not _is_valid_video_content(filepath):
                        _cleanup_file(filepath)
                        return False, "Downloaded file is not a valid video", 0

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


# ─── Download: Public API ──────────────────────────────────


async def download_hentaihaven_direct(
    url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود لینک مستقیم MP4."""
    # yt-dlp اول (بهترین برای streaming servers)
    if shutil.which("yt-dlp"):
        logger.info("Trying download with yt-dlp: %s", url[:80])
        success, error, size = await _download_with_ytdlp(url, filepath, progress_cb)
        if success:
            return True, "", size
        logger.info("yt-dlp download failed: %s", error)
        _cleanup_file(filepath)

    # curl_cffi
    if _check_impersonation_support():
        logger.info("Trying download with curl_cffi: %s", url[:80])
        success, error, size = await _download_with_curl_cffi(url, filepath, progress_cb)
        if success:
            return True, "", size
        logger.info("curl_cffi download failed: %s", error)
        _cleanup_file(filepath)

    # aiohttp
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
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp is not installed", 0

    success, error, size = await _download_with_ytdlp(m3u8_url, filepath, progress_cb)
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error, 0
