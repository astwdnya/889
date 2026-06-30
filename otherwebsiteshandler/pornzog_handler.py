"""
pornzog_handler.py
------------------
استخراج لینک‌های دانلود از pornzog.com و ارسال ویدیو به کاربر.

روش کار:
  - pornzog فقط یه iframe wrapper هست، ویدیو واقعی روی سایت‌های
    دیگه (privatehomeclips, txxx, ...) هاست شده
  - از yt-dlp با impersonation استفاده میکنه (بهترین روش)
  - اگه yt-dlp فشل شد، iframe URL رو استخراج و مستقیم پارس میکنه
  - دانلود با curl_cffi/yt-dlp/aiohttp (به ترتیب اولویت)
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse, urljoin, unquote

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

# ─── Constants ──────────────────────────────────────────────

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_RETRIES = 3
RETRY_DELAY = 2.0
PROGRESS_INTERVAL = 2.0
YTDLP_TIMEOUT = 90
DOWNLOAD_READ_TIMEOUT = 120

_ALLOWED_HOSTS = frozenset({"pornzog.com", "www.pornzog.com"})

# سایت‌های embed که pornzog ازشون استفاده میکنه + CDN ها
_ALLOWED_EMBED_HOSTS = frozenset(
    {
        "privatehomeclips.com",
        "www.privatehomeclips.com",
        "txxx.com",
        "www.txxx.com",
        "voyeurhit.com",
        "www.voyeurhit.com",
        "hclips.com",
        "www.hclips.com",
        "hdzog.com",
        "www.hdzog.com",
        "tporn.xxx",
        "www.tporn.xxx",
        "upornia.com",
        "www.upornia.com",
    }
)

_ALLOWED_CDN_SUFFIXES = (
    ".pornzog.com",
    ".privatehomeclips.com",
    ".txxx.com",
    ".voyeurhit.com",
    ".hclips.com",
    ".hdzog.com",
    ".tporn.xxx",
    ".upornia.com",
    ".cdntrex.com",
    ".gvideo.io",
    ".googleapis.com",
    ".googleusercontent.com",
    ".cdn13.com",
    ".betacdn.net",
    ".bcdn.cc",
    ".b-cdn.net",
    ".mxdcontent.net",
    ".kvs-demo.com",
)

_IMPERSONATE_TARGETS = [
    "chrome",
    "chrome:120",
    "chrome:110",
    "edge",
    "safari",
]

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Utility ────────────────────────────────────────────────


def is_pornzog_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به pornzog هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(".pornzog.com")
    except Exception:
        return False


def _is_allowed_host(url: str) -> bool:
    """بررسی اینکه URL به دامنه‌های مجاز اشاره میکنه."""
    try:
        host = urlparse(url).hostname or ""
        return (
            host in _ALLOWED_HOSTS
            or host in _ALLOWED_EMBED_HOSTS
            or any(host.endswith(s) for s in _ALLOWED_CDN_SUFFIXES)
        )
    except Exception:
        return False


def _is_video_cdn_url(url: str) -> bool:
    """بررسی اینکه URL یه لینک ویدیو CDN هست."""
    if _is_allowed_host(url):
        return True
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        if parsed.scheme == "https" and (
            path.endswith(".mp4")
            or path.endswith(".m3u8")
            or "/get_file/" in path
            or "/video/" in path
            or "/media/" in path
            or "/hls/" in path
        ):
            return True
    except Exception:
        pass
    return False


def _normalize_url(url: str, base_url: str = "") -> Optional[str]:
    """نرمال‌سازی URL."""
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


def _cleanup_file(filepath: str) -> None:
    """حذف فایل اگه وجود داشته باشه."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            logger.debug("Cleaned up file: %s", filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _format_progress(
    downloaded: int, content_length: int, start_time: float, now: float
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
    """بررسی اینکه curl_cffi نصبه."""
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
    """ساخت aiohttp session."""
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
    """دریافت URL با retry."""
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
            last_error = str(e)[:200]
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


# ─── iframe extraction ─────────────────────────────────────


def _extract_embed_urls(html: str) -> List[str]:
    """
    استخراج URL های embed/iframe از صفحه pornzog.
    pornzog ویدیو رو توی iframe از سایت‌های دیگه لود میکنه.
    """
    urls = []

    # iframe src
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
        src = m.group(1).strip()
        if not src or "pornzog.com/embed" in src:
            # embed خود pornzog رو رد کن، اون هم redirect میکنه
            # ولی بذارش آخر لیست
            urls.append(src)
            continue
        parsed = urlparse(src)
        host = parsed.hostname or ""
        if host in _ALLOWED_EMBED_HOSTS or any(
            host.endswith(s) for s in _ALLOWED_CDN_SUFFIXES
        ):
            urls.insert(0, src)  # اولویت بالاتر
        elif host and "embed" in src.lower():
            urls.append(src)

    # data-src (lazy load)
    for m in re.finditer(
        r'<iframe[^>]+data-src=["\']([^"\']+)["\']', html, re.IGNORECASE
    ):
        src = m.group(1).strip()
        if src and src not in urls:
            urls.append(src)

    return urls


# ─── yt-dlp extraction ─────────────────────────────────────


async def _extract_with_ytdlp(url: str) -> Tuple[List[dict], str]:
    """
    استخراج لینک‌های ویدیو با yt-dlp.
    yt-dlp خودش iframe رو follow میکنه و لینک نهایی رو میده.
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
        # اگه 403 یا Cloudflare نبود، بقیه رو امتحان نکن
        if "HTTP Error 403" not in error and "Cloudflare" not in error:
            break

    return [], error if error else "Extraction failed"


async def _try_ytdlp_extract(
    url: str, method: str, target: Optional[str]
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
        logger.info("yt-dlp: trying --impersonate %s", target)
    elif method == "extractor-args":
        cmd.extend(["--extractor-args", "generic:impersonate=chrome"])
        logger.info("yt-dlp: trying extractor-args impersonate")
    else:
        logger.info("yt-dlp: trying basic (no impersonation)")

    cmd.append(url)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=YTDLP_TIMEOUT
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
        return [], "", str(e)[:200]


def _parse_ytdlp_formats(data: dict) -> List[dict]:
    """پردازش فرمت‌های yt-dlp."""
    qualities: List[dict] = []
    seen_urls = set()

    formats = data.get("formats", [])
    if not formats:
        # فقط یه URL مستقیم
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
        format_id = fmt.get("format_id", "")
        filesize = fmt.get("filesize") or fmt.get("filesize_approx") or 0

        # فقط audio رو رد کن
        if vcodec == "none":
            continue

        is_m3u8 = (
            protocol in ("m3u8", "m3u8_native") or ".m3u8" in fmt_url or ext == "m3u8"
        )
        size_str = f" ({filesize / 1024 / 1024:.0f}MB)" if filesize else ""

        if height:
            prefix = "📡 M3U8" if is_m3u8 else f"🎥 {ext.upper()}"
            label = f"{prefix} {height}p{size_str}"
        elif format_note:
            label = f"🎥 {format_note}{size_str}"
        elif format_id:
            label = f"🎥 {format_id}{size_str}"
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


# ─── Embed page extraction (fallback) ──────────────────────


async def _extract_from_embed_page(embed_url: str) -> Tuple[List[dict], str]:
    """
    استخراج لینک ویدیو از صفحه embed (وقتی yt-dlp فشل شد).
    سایت‌های KVS مثل privatehomeclips از flashvars استفاده میکنن.
    """
    # اول با curl_cffi
    html = None
    if _check_impersonation_support():
        html = await _fetch_embed_curl_cffi(embed_url)

    # بعد با aiohttp
    if not html:
        text, status = await _fetch_with_retry(
            embed_url,
            headers={"Referer": "https://www.pornzog.com/"},
        )
        html = text

    if not html:
        return [], "Could not fetch embed page"

    title = _extract_title_from_html(html)
    qualities: List[dict] = []

    # KVS flashvars
    _extract_kvs_flashvars(html, embed_url, qualities)

    # video/source tags
    _extract_video_source_tags(html, embed_url, qualities)

    # JS variables
    _extract_js_video_urls(html, embed_url, qualities)

    # JSON-LD
    _extract_json_ld(html, embed_url, qualities)

    return qualities, title


async def _fetch_embed_curl_cffi(url: str) -> Optional[str]:
    """دریافت صفحه embed با curl_cffi."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None

    try:
        async with AsyncSession() as session:
            resp = await session.get(
                url,
                impersonate="chrome",
                headers={
                    "Referer": "https://www.pornzog.com/",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                allow_redirects=True,
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.text
            logger.debug("curl_cffi embed fetch: HTTP %d", resp.status_code)
    except Exception as e:
        logger.debug("curl_cffi embed fetch failed: %s", e)

    return None


# ─── HTML parsing helpers ──────────────────────────────────


def _extract_title_from_html(html: str) -> str:
    """استخراج عنوان ویدیو."""
    # og:title
    m = re.search(
        r'(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:title["\']',
            html,
            re.IGNORECASE,
        )
    if m:
        return m.group(1).strip()

    # <title>
    m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*[Pp]ornzog.*$", "", title).strip()
        return title or "Untitled"

    # <h1>
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return "Untitled"


def _extract_kvs_flashvars(html: str, page_url: str, qualities: List[dict]) -> None:
    """
    استخراج از KVS flashvars.
    privatehomeclips و سایت‌های مشابه از این فرمت استفاده میکنن.
    """
    # فرمت 1: var flashvars = {...};
    for m in re.finditer(r"var\s+flashvars\s*=\s*(\{[^;]+?\});", html, re.DOTALL):
        _parse_flashvars_block(m.group(1), page_url, qualities, html)

    # فرمت 2: var flashvars_XXXXX = {...};
    for m in re.finditer(r"var\s+flashvars_\w+\s*=\s*(\{[^;]+?\});", html, re.DOTALL):
        _parse_flashvars_block(m.group(1), page_url, qualities, html)

    # فرمت 3: kt_player('...', '...', {...})
    for m in re.finditer(
        r"kt_player\s*\([^,]+,\s*['\"][^'\"]+['\"]\s*,\s*(\{.+?\})\s*\)",
        html,
        re.DOTALL,
    ):
        _parse_flashvars_block(m.group(1), page_url, qualities, html)

    # فرمت 4: flashvars['key'] = 'value'
    fv_items = {}
    for m in re.finditer(r"flashvars\[(['\"])(\w+)\1\]\s*=\s*(['\"])([^'\"]*)\3", html):
        fv_items[m.group(2)] = m.group(4)

    if fv_items:
        _process_kvs_url_dict(fv_items, page_url, qualities, html)


def _parse_flashvars_block(
    raw: str, page_url: str, qualities: List[dict], full_html: str
) -> None:
    """پارس یک بلاک flashvars."""
    # سعی کن JSON پارس کنی
    try:
        clean = re.sub(r"'", '"', raw)
        clean = re.sub(r",\s*}", "}", clean)
        clean = re.sub(r",\s*]", "]", clean)
        data = json.loads(clean)
        if isinstance(data, dict):
            _process_kvs_url_dict(data, page_url, qualities, full_html)
            return
    except (json.JSONDecodeError, ValueError):
        pass

    # با regex استخراج کن
    extracted = {}
    for key in [
        "video_url",
        "video_alt_url",
        "video_alt_url2",
        "video_alt_url3",
        "video_url_text",
        "video_alt_url_text",
        "video_alt_url2_text",
        "license_code",
    ]:
        m = re.search(rf"""['"]{key}['"]\s*:\s*['"]([^'"]*?)['"]""", raw)
        if not m:
            m = re.search(rf"""{key}\s*:\s*['"]([^'"]*?)['"]""", raw)
        if m:
            extracted[key] = m.group(1)

    if extracted:
        _process_kvs_url_dict(extracted, page_url, qualities, full_html)


def _process_kvs_url_dict(
    data: dict, page_url: str, qualities: List[dict], full_html: str
) -> None:
    """پردازش dict حاوی URL های KVS."""
    url_keys = {
        "video_url": "Default",
        "video_alt_url": "720p",
        "video_alt_url2": "480p",
        "video_alt_url3": "360p",
    }
    label_keys = {
        "video_url_text": "video_url",
        "video_alt_url_text": "video_alt_url",
        "video_alt_url2_text": "video_alt_url2",
    }

    # label ها
    labels = {}
    for lk, uk in label_keys.items():
        if lk in data and data[lk]:
            labels[url_keys.get(uk, "Default")] = data[lk]

    license_code = data.get("license_code", "")

    for key, quality_name in url_keys.items():
        raw_url = data.get(key, "")
        if not raw_url or not raw_url.startswith("http"):
            continue

        video_url = _normalize_url(raw_url, page_url)
        if not video_url:
            continue

        # KVS license_code decoding
        if license_code and "/get_file/" in video_url:
            video_url = _decode_kvs_url(video_url, license_code)

        if any(q["url"] == video_url for q in qualities):
            continue

        label_text = labels.get(quality_name, "")
        if label_text:
            label = f"🎥 MP4 {label_text}"
        else:
            res = re.search(r"(\d{3,4})p", video_url)
            if res:
                label = f"🎥 MP4 {res.group(1)}p"
            else:
                label = f"🎥 MP4 ({quality_name})"

        qualities.append(
            {
                "label": label,
                "url": video_url,
                "method": "direct",
            }
        )


def _decode_kvs_url(url: str, license_code: str) -> str:
    """دیکد URL با license_code (الگوریتم KVS)."""
    try:
        parts = license_code.split("$")
        if len(parts) < 2:
            return url

        parsed = urlparse(url)
        path_parts = parsed.path.split("/")

        for i, part in enumerate(path_parts):
            if len(part) == 32 and re.match(r"^[0-9a-f]+$", part):
                if i + 1 < len(path_parts):
                    key = path_parts[i + 1]
                    decoded = []
                    for j, ch in enumerate(key):
                        if ch.isdigit():
                            idx = j % len(parts[1])
                            new_d = (int(ch) + int(parts[1][idx])) % 10
                            decoded.append(str(new_d))
                        else:
                            decoded.append(ch)
                    path_parts[i + 1] = "".join(decoded)
                    new_path = "/".join(path_parts)
                    return url.replace(parsed.path, new_path)
                break
    except Exception as e:
        logger.debug("KVS URL decode failed: %s", e)

    return url


def _extract_video_source_tags(html: str, page_url: str, qualities: List[dict]) -> None:
    """استخراج از <video> و <source> tags."""
    # video src
    for m in re.finditer(r'<video[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
        video_url = _normalize_url(m.group(1), page_url)
        if video_url and _is_video_cdn_url(video_url):
            if not any(q["url"] == video_url for q in qualities):
                qualities.append(
                    {
                        "label": "🎥 MP4 (video tag)",
                        "url": video_url,
                        "method": "direct",
                    }
                )

    # source tags
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

        if label_m:
            q_label = label_m.group(1)
        elif res_m:
            q_label = f"{res_m.group(1)}p"
        else:
            url_res = re.search(r"(\d{3,4})p", src)
            q_label = f"{url_res.group(1)}p" if url_res else "Default"

        prefix = "📡 M3U8" if is_m3u8 else "🎥 MP4"
        qualities.append(
            {
                "label": f"{prefix} {q_label}",
                "url": src,
                "method": "m3u8" if is_m3u8 else "direct",
            }
        )


def _extract_js_video_urls(html: str, page_url: str, qualities: List[dict]) -> None:
    """استخراج لینک ویدیو از متغیرهای JS."""
    patterns = [
        (r"""video_url\s*[:=]\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""video[_]?[Ff]ile\s*[:=]\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""file\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""src\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", None),
        (r"""['"]?(\d{3,4})['"]?\s*:\s*['"]([^'"]+\.mp4[^'"]*)['"]""", "numbered"),
    ]

    for pattern, ptype in patterns:
        for m in re.finditer(pattern, html):
            if ptype == "numbered":
                quality_num = m.group(1)
                raw_url = m.group(2)
                label = f"🎥 MP4 {quality_num}p"
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


def _extract_json_ld(html: str, page_url: str, qualities: List[dict]) -> None:
    """استخراج از JSON-LD."""
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


# ─── Main extraction ───────────────────────────────────────


async def extract_pornzog_qualities(
    url: str,
    debug_cb: Optional[Callable[[str], None]] = None,
) -> Tuple[List[dict], str]:
    """
    لینک‌های کیفیت مختلف رو از صفحه pornzog استخراج میکنه.

    استراتژی:
      1. yt-dlp (خودش iframe رو follow میکنه)
      2. iframe URL استخراج + embed page پارس
      3. yt-dlp روی هر embed URL جداگانه
    """
    if not is_pornzog_url(url):
        logger.warning("Not a valid pornzog URL: %s", url)
        return [], "Invalid URL"

    if debug_cb:
        debug_cb("🔍 Method 1: yt-dlp direct...")

    # ── روش 1: yt-dlp مستقیم (بهترین روش) ──
    logger.info("Method 1: yt-dlp direct for %s", url)
    qualities, title = await _extract_with_ytdlp(url)
    if qualities:
        qualities.sort(key=_quality_sort_key, reverse=True)
        logger.info("Extracted %d qualities via yt-dlp", len(qualities))
        if debug_cb:
            debug_cb(f"✅ Found {len(qualities)} qualities via yt-dlp")
        return qualities, title

    if debug_cb:
        debug_cb("🔍 Method 2: iframe extraction...")

    # ── روش 2: iframe استخراج + embed page پارس ──
    logger.info("Method 2: iframe extraction for %s", url)
    page_html = None

    if _check_impersonation_support():
        page_html = await _fetch_embed_curl_cffi(url)

    if not page_html:
        page_html, _ = await _fetch_with_retry(url)

    if page_html:
        title = _extract_title_from_html(page_html)
        embed_urls = _extract_embed_urls(page_html)
        logger.info("Found %d embed URLs", len(embed_urls))
        if debug_cb:
            debug_cb(f"🔍 Found {len(embed_urls)} embed URLs")

        for i, embed_url in enumerate(embed_urls):
            if debug_cb:
                debug_cb(
                    f"🔍 Trying embed [{i + 1}/{len(embed_urls)}]: {embed_url[:60]}..."
                )

            # اول yt-dlp روی embed URL
            eq, et = await _extract_with_ytdlp(embed_url)
            if eq:
                eq.sort(key=_quality_sort_key, reverse=True)
                final_title = et if et and et != "Untitled" else title
                logger.info("Extracted %d qualities from embed via yt-dlp", len(eq))
                if debug_cb:
                    debug_cb(f"✅ Found {len(eq)} qualities via yt-dlp on embed")
                return eq, final_title

            # بعد مستقیم embed page رو پارس کن
            eq, et = await _extract_from_embed_page(embed_url)
            if eq:
                eq.sort(key=_quality_sort_key, reverse=True)
                final_title = et if et and et != "Untitled" else title
                logger.info("Extracted %d qualities from embed page", len(eq))
                if debug_cb:
                    debug_cb(f"✅ Found {len(eq)} qualities via embed page parsing")
                return eq, final_title

    logger.warning("All extraction methods failed for: %s", url)
    if debug_cb:
        debug_cb("❌ All extraction methods failed")

    has_ytdlp = shutil.which("yt-dlp") is not None
    has_curl_cffi = _check_impersonation_support()

    if not has_ytdlp:
        return [], "yt-dlp is not installed. Install: pip install yt-dlp"
    if not has_curl_cffi:
        return [], (
            "Install impersonation support:\n"
            "pip install curl_cffi\n"
            "or: pip install yt-dlp[default,curl-cffi]"
        )

    return [], "Extraction failed - site may have updated its protection"


# ─── Download ──────────────────────────────────────────────


# ─── Download: Multi-segment (fast) ────────────────────────


async def _download_multi_segment(
    url: str,
    filepath: str,
    referer: str,
    progress_cb: ProgressCallback,
    num_segments: int = 8,
) -> Tuple[bool, str, int]:
    """
    دانلود چند تیکه‌ای با چند connection همزمان.
    هر تیکه یه Range request جدا میزنه → سرعت N برابر.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        async with AsyncSession() as session:
            head_resp = await session.head(
                url,
                impersonate="chrome",
                headers={"Referer": referer},
                allow_redirects=True,
                timeout=15,
            )

            content_length = int(head_resp.headers.get("Content-Length", 0))
            accept_ranges = head_resp.headers.get("Accept-Ranges", "")

            if content_length == 0:
                return False, "Cannot determine file size", 0

            if content_length > MAX_DOWNLOAD_SIZE:
                return (
                    False,
                    (f"File too large: {content_length / 1024 / 1024:.0f} MB"),
                    0,
                )

            if accept_ranges.lower() != "bytes":
                logger.info("Server doesn't support Range requests, falling back")
                return False, "Range not supported", 0

        total_mb = content_length / 1024 / 1024
        await progress_cb(
            f"📥 **دانلود چند تیکه‌ای ({num_segments} بخش)...**\n"
            f"💾 حجم: {total_mb:.1f} MB"
        )

        segment_size = content_length // num_segments
        segments = []
        for i in range(num_segments):
            start = i * segment_size
            end = (
                content_length - 1
                if i == num_segments - 1
                else (i + 1) * segment_size - 1
            )
            segments.append((i, start, end))

        segment_files = [f"{filepath}.part{i}" for i in range(num_segments)]
        downloaded_bytes = [0] * num_segments
        start_time = time.time()
        last_update = [0.0]
        lock = asyncio.Lock()

        async def _download_segment(seg_idx: int, byte_start: int, byte_end: int):
            seg_file = segment_files[seg_idx]
            for attempt in range(MAX_RETRIES):
                try:
                    async with AsyncSession() as session:
                        resp = await session.get(
                            url,
                            impersonate="chrome",
                            headers={
                                "Referer": referer,
                                "Range": f"bytes={byte_start}-{byte_end}",
                                "Accept": "*/*",
                                "Accept-Language": "en-US,en;q=0.9",
                            },
                            allow_redirects=True,
                            timeout=300,
                            stream=True,
                        )

                        if resp.status_code not in (200, 206):
                            raise Exception(f"HTTP {resp.status_code}")

                        async with aiofiles.open(seg_file, "wb") as f:
                            async for chunk in resp.aiter_content():
                                if not chunk:
                                    continue
                                await f.write(chunk)
                                downloaded_bytes[seg_idx] += len(chunk)

                                now = time.time()
                                async with lock:
                                    if now - last_update[0] >= PROGRESS_INTERVAL:
                                        last_update[0] = now
                                        total_dl = sum(downloaded_bytes)
                                        await progress_cb(
                                            _format_progress(
                                                total_dl,
                                                content_length,
                                                start_time,
                                                now,
                                            )
                                        )
                        return

                except asyncio.CancelledError:
                    _cleanup_file(seg_file)
                    raise
                except Exception as e:
                    logger.warning(
                        "Segment %d attempt %d failed: %s",
                        seg_idx,
                        attempt + 1,
                        e,
                    )
                    _cleanup_file(seg_file)
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))

            raise Exception(f"Segment {seg_idx} failed after {MAX_RETRIES} attempts")

        try:
            await asyncio.gather(
                *[_download_segment(idx, start, end) for idx, start, end in segments]
            )
        except Exception as e:
            for sf in segment_files:
                _cleanup_file(sf)
            return False, str(e)[:200], 0

        await progress_cb("🔗 **ترکیب بخش‌ها...**")
        try:
            async with aiofiles.open(filepath, "wb") as outfile:
                for sf in segment_files:
                    if not os.path.exists(sf):
                        raise FileNotFoundError(f"Missing segment: {sf}")
                    async with aiofiles.open(sf, "rb") as infile:
                        while True:
                            chunk = await infile.read(4 * 1024 * 1024)
                            if not chunk:
                                break
                            await outfile.write(chunk)
        finally:
            for sf in segment_files:
                _cleanup_file(sf)

        if not os.path.exists(filepath):
            return False, "Merged file not created", 0

        final_size = os.path.getsize(filepath)
        if final_size == 0:
            _cleanup_file(filepath)
            return False, "Merged file is empty", 0

        if abs(final_size - content_length) > 1024:
            logger.warning(
                "Size mismatch: expected %d, got %d",
                content_length,
                final_size,
            )

        elapsed = time.time() - start_time
        avg_speed = final_size / elapsed / 1024 / 1024 if elapsed > 0 else 0
        logger.info(
            "Multi-segment download complete: %.1f MB in %.1fs (%.1f MB/s)",
            final_size / 1024 / 1024,
            elapsed,
            avg_speed,
        )

        return True, "", final_size

    except Exception as e:
        logger.warning("Multi-segment download error: %s", e)
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


async def _download_with_curl_cffi(
    url: str, filepath: str, referer: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """دانلود با curl_cffi."""
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
                    "Referer": referer,
                    "Origin": urlparse(referer).scheme
                    + "://"
                    + urlparse(referer).hostname,
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
                    if now - last_update >= PROGRESS_INTERVAL:
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
        return False, str(e)[:200], 0


async def _download_with_ytdlp(
    url: str, filepath: str, referer: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """دانلود با yt-dlp."""
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
            f"Referer:{referer}",
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
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=DOWNLOAD_READ_TIMEOUT
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                _cleanup_file(filepath)
                return False, "Download timed out", 0

            if not line:
                break

            text = line.decode(errors="replace").strip()
            now = time.time()
            if now - last_update >= PROGRESS_INTERVAL and text:
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
        return False, str(e)[:200], 0


async def _download_with_aiohttp(
    url: str, filepath: str, referer: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """دانلود با aiohttp ساده."""
    headers = {
        **_DEFAULT_HEADERS,
        "Referer": referer,
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
                                if now - last_update >= PROGRESS_INTERVAL:
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
            error = str(e)[:200]

        if attempt < MAX_RETRIES:
            _cleanup_file(filepath)
            await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)
    return False, f"Failed after {MAX_RETRIES} attempts: {error}", 0


# ─── Download: Public API ──────────────────────────────────


def _guess_referer(url: str) -> str:
    """حدس Referer مناسب بر اساس host لینک دانلود."""
    try:
        host = urlparse(url).hostname or ""
        if "privatehomeclips" in host:
            return "https://www.privatehomeclips.com/"
        if "txxx" in host:
            return "https://www.txxx.com/"
        if "pornzog" in host:
            return "https://www.pornzog.com/"
        # default: از همون host
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname}/"
    except Exception:
        return "https://www.pornzog.com/"


async def download_pornzog_direct(
    url: str, filepath: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """
    دانلود لینک مستقیم MP4.
    اول multi-segment، بعد curl_cffi، بعد yt-dlp، آخر aiohttp.
    """
    if not _is_video_cdn_url(url):
        return False, "URL host not allowed", 0

    referer = _guess_referer(url)

    # ── روش 1: دانلود چند تیکه‌ای (سریع‌ترین) ──
    if _check_impersonation_support():
        logger.info("Download attempt 1: multi-segment (8 connections)")
        success, error, size = await _download_multi_segment(
            url, filepath, referer, progress_cb, num_segments=8
        )
        if success:
            return True, "", size
        logger.info("Multi-segment failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 2: curl_cffi تک connection ──
    if _check_impersonation_support():
        logger.info("Download attempt 2: curl_cffi single")
        success, error, size = await _download_with_curl_cffi(
            url, filepath, referer, progress_cb
        )
        if success:
            return True, "", size
        logger.info("curl_cffi download failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 3: yt-dlp ──
    if shutil.which("yt-dlp"):
        logger.info("Download attempt 3: yt-dlp")
        success, error, size = await _download_with_ytdlp(
            url, filepath, referer, progress_cb
        )
        if success:
            return True, "", size
        logger.info("yt-dlp download failed: %s", error)
        _cleanup_file(filepath)

    # ── روش 4: aiohttp ──
    logger.info("Download attempt 4: aiohttp")
    success, error, size = await _download_with_aiohttp(
        url, filepath, referer, progress_cb
    )
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0


async def download_pornzog_m3u8(
    url: str, filepath: str, progress_cb: ProgressCallback
) -> Tuple[bool, str, int]:
    """دانلود M3U8 stream."""
    if not _is_video_cdn_url(url):
        return False, "URL host not allowed", 0

    if not shutil.which("yt-dlp"):
        return False, "yt-dlp is not installed", 0

    referer = _guess_referer(url)
    success, error, size = await _download_with_ytdlp(
        url, filepath, referer, progress_cb
    )
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0
