"""
xanimu_handler.py (DEBUG Edition)
─────────────────────────────────
نسخه دیباگ کامل - تمام مراحل رو توی تلگرام نشون میده
"""

import asyncio
import html as html_lib
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("XanimuHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
MAX_RETRIES = 3
RETRY_DELAY = 2.0
CURL_TIMEOUT = 30
MIN_FILE_SIZE = 1024
CHUNK_SIZE = 256 * 1024
PROGRESS_INTERVAL = 2.0
MAX_SPEED_DISPLAY = 99999

_ALLOWED_HOSTS = frozenset({"xanimu.com", "www.xanimu.com"})

_CDN_HOSTS = frozenset({
    "xcdn1.nosofiles.com",
    "xcdn2.nosofiles.com",
    "st1.nosofiles.com",
    "st2.nosofiles.com",
})

ProgressCallback = Callable[[str], Awaitable[None]]


# ─── Debug Logger ───────────────────────────────────────────


@dataclass
class DebugLog:
    """جمع‌آوری لاگ‌های دیباگ برای ارسال به تلگرام."""

    entries: List[str] = field(default_factory=list)
    _start_time: float = field(default_factory=time.time)

    def add(self, emoji: str, message: str) -> None:
        elapsed = time.time() - self._start_time
        entry = f"{emoji} [{elapsed:.1f}s] {message}"
        self.entries.append(entry)
        logger.debug(entry)

    def ok(self, msg: str) -> None:
        self.add("✅", msg)

    def warn(self, msg: str) -> None:
        self.add("⚠️", msg)

    def err(self, msg: str) -> None:
        self.add("❌", msg)

    def info(self, msg: str) -> None:
        self.add("ℹ️", msg)

    def step(self, msg: str) -> None:
        self.add("🔍", msg)

    def build_report(self, title: str = "Debug Report") -> str:
        header = f"🐛 **{title}**\n{'─' * 30}\n"
        body = "\n".join(self.entries)
        return f"{header}{body}"

    def build_short(self, max_entries: int = 30) -> str:
        """نسخه کوتاه برای تلگرام (محدودیت 4096 کاراکتر)."""
        header = "🐛 **Debug Report**\n\n"
        selected = self.entries[-max_entries:]
        body = "\n".join(selected)
        result = f"{header}{body}"
        if len(result) > 4000:
            result = result[:3990] + "\n..."
        return result


# ─── Utility ────────────────────────────────────────────────


def is_xanimu_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS
    except Exception:
        return False


def _is_valid_cdn_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _CDN_HOSTS or host in _ALLOWED_HOSTS
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


def _check_curl() -> bool:
    return shutil.which("curl") is not None


# ─── HTTP: curl subprocess ──────────────────────────────────


async def _fetch_with_curl(url: str, dbg: DebugLog) -> Tuple[Optional[str], int]:
    if not _check_curl():
        dbg.err("curl پیدا نشد در PATH")
        return None, 0

    dbg.ok("curl پیدا شد")

    cmd = [
        "curl", "-s",
        "-w", "\n__HTTP_CODE__%{http_code}",
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-L",
        "--compressed",
        "--max-time", str(CURL_TIMEOUT),
        url,
    ]

    dbg.step(f"curl command: `curl -s -L --compressed {url}`")

    for attempt in range(1, MAX_RETRIES + 1):
        dbg.step(f"curl تلاش {attempt}/{MAX_RETRIES}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=CURL_TIMEOUT + 10,
            )

            stderr_text = stderr.decode(errors="replace").strip()
            if stderr_text:
                dbg.warn(f"curl stderr: `{stderr_text[:200]}`")

            output = stdout.decode(errors="replace")
            dbg.info(f"curl output size: {len(output)} bytes")

            code_marker = "__HTTP_CODE__"
            if code_marker in output:
                parts = output.rsplit(code_marker, 1)
                html = parts[0]
                try:
                    status = int(parts[1].strip())
                except (ValueError, IndexError):
                    status = 0
                dbg.info(f"HTTP status: {status}")
            else:
                html = output
                status = 200 if process.returncode == 0 else 0
                dbg.warn(f"HTTP code marker نبود، return code: {process.returncode}")

            dbg.info(f"HTML size: {len(html)} chars")

            # نمونه اول HTML
            preview = html[:500].replace("\n", " ").strip()
            dbg.step(f"HTML preview: `{preview[:300]}...`")

            # بررسی Cloudflare
            if "Just a moment" in html:
                dbg.err("⛔ Cloudflare challenge detected!")
                dbg.info(f"HTML contains 'Just a moment' at position {html.index('Just a moment')}")

            if "cf-browser-verification" in html:
                dbg.err("⛔ Cloudflare browser verification detected!")

            if "challenge-platform" in html:
                dbg.err("⛔ Cloudflare challenge platform detected!")

            if "<title>Attention Required" in html:
                dbg.err("⛔ Cloudflare Attention Required page!")

            # بررسی redirect
            if "window.location" in html or "meta http-equiv=\"refresh\"" in html.lower():
                dbg.warn("Redirect detected in HTML")

            if status == 200 and len(html) > 1000:
                dbg.ok(f"صفحه دریافت شد: {len(html)} chars, status {status}")
                return html, 200

            if 400 <= status < 500:
                dbg.err(f"Client error: HTTP {status}")
                return None, status

            dbg.warn(f"تلاش ناموفق: status={status}, size={len(html)}")

        except asyncio.TimeoutError:
            dbg.err(f"curl timeout در تلاش {attempt}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            dbg.err(f"curl exception: {type(e).__name__}: {e}")

        if attempt < MAX_RETRIES:
            wait = RETRY_DELAY * attempt
            dbg.info(f"صبر {wait}s قبل از تلاش بعدی...")
            await asyncio.sleep(wait)

    dbg.err(f"curl بعد از {MAX_RETRIES} تلاش فیل شد")
    return None, 0


# ─── Extraction ─────────────────────────────────────────────


def _extract_from_html(html: str, page_url: str, dbg: DebugLog) -> Tuple[List[dict], str]:
    title = _extract_title(html, dbg)
    qualities: List[dict] = []
    seen_urls: set = set()

    dbg.step("── شروع استخراج کیفیت‌ها ──")

    # روش 1: JS variables
    dbg.step("روش 1: جستجوی JS variables (videoHigh/videoLow)")
    _extract_js_vars(html, qualities, seen_urls, dbg)

    # روش 2: video/source tags
    dbg.step("روش 2: جستجوی <video> و <source> tags")
    _extract_video_tags(html, qualities, seen_urls, dbg)

    # روش 3: MP4 URLs مستقیم
    dbg.step("روش 3: جستجوی MP4 URLs مستقیم")
    _extract_direct_mp4(html, qualities, seen_urls, dbg)

    # روش 4 (جدید): جستجوی هر URL با پسوند mp4
    dbg.step("روش 4: جستجوی عمومی هر لینک MP4")
    _extract_any_mp4(html, qualities, seen_urls, dbg)

    # روش 5 (جدید): جستجوی JSON/object حاوی URL
    dbg.step("روش 5: جستجوی JSON objects حاوی video URL")
    _extract_json_sources(html, qualities, seen_urls, dbg)

    qualities.sort(key=lambda q: q.get("height", 0), reverse=True)

    dbg.info(f"مجموع کیفیت‌های پیدا شده: {len(qualities)}")
    for i, q in enumerate(qualities):
        dbg.ok(f"  کیفیت {i+1}: {q['label']} → `{q['url'][:80]}...`")

    return qualities, title


def _extract_title(html: str, dbg: DebugLog) -> str:
    dbg.step("استخراج عنوان...")

    # toStore JSON
    ts_m = re.search(r'"title"\s*:\s*"([^"]+)"', html)
    if ts_m:
        title = ts_m.group(1).strip()
        if len(title) > 3:
            dbg.ok(f"عنوان از JSON: `{title[:60]}`")
            return html_lib.unescape(title)

    # <title>
    t_m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if t_m:
        title = t_m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*XAnimu\.com\s*$", "", title, flags=re.I).strip()
        if title:
            dbg.ok(f"عنوان از <title>: `{title[:60]}`")
            return html_lib.unescape(title)

    # og:title
    og_m = re.search(r'property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.I)
    if og_m:
        title = html_lib.unescape(og_m.group(1).strip())
        dbg.ok(f"عنوان از og:title: `{title[:60]}`")
        return title

    # h1
    h1_m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    if h1_m:
        title = html_lib.unescape(h1_m.group(1).strip())
        dbg.ok(f"عنوان از h1: `{title[:60]}`")
        return title

    dbg.warn("عنوان پیدا نشد، استفاده از 'Untitled'")
    return "Untitled"


def _extract_js_vars(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    # جستجوی تمام var definitions مرتبط با video
    all_vars = re.findall(r'var\s+(\w*[Vv]ideo\w*)\s*=\s*"([^"]*)"', html)
    if all_vars:
        dbg.info(f"JS video vars پیدا شد: {len(all_vars)}")
        for name, val in all_vars:
            dbg.info(f"  var {name} = `{val[:100]}`")
    else:
        dbg.warn("هیچ JS video var پیدا نشد")

        # جستجوی گسترده‌تر
        any_vars = re.findall(r'var\s+(\w+)\s*=\s*"(https?://[^"]+)"', html)
        if any_vars:
            dbg.info(f"سایر JS vars با URL: {len(any_vars)}")
            for name, val in any_vars[:10]:
                dbg.info(f"  var {name} = `{val[:100]}`")

        # جستجوی let/const
        let_vars = re.findall(r'(?:let|const)\s+(\w*[Vv]ideo\w*)\s*=\s*["\']([^"\']*)["\']', html)
        if let_vars:
            dbg.info(f"let/const video vars: {len(let_vars)}")
            for name, val in let_vars:
                dbg.info(f"  {name} = `{val[:100]}`")

    # videoHigh
    high_m = re.search(r'var\s+videoHigh\s*=\s*"([^"]+)"', html)
    if high_m:
        url = high_m.group(1).strip()
        dbg.ok(f"videoHigh پیدا شد: `{url[:80]}`")

        if not _is_valid_cdn_url(url):
            dbg.err(f"videoHigh URL نامعتبر (CDN check failed): host={urlparse(url).hostname}")
        elif url in seen_urls:
            dbg.warn("videoHigh تکراری")
        else:
            seen_urls.add(url)
            high_title_m = re.search(r'var\s+videoHighTitle\s*=\s*"([^"]+)"', html)
            label_text = high_title_m.group(1) if high_title_m else "High"
            height = _parse_height(label_text) or 720
            dbg.ok(f"videoHigh اضافه شد: {label_text}, {height}p")
            qualities.append({
                "label": f"📺 {label_text} (High Quality)",
                "url": url,
                "method": "direct",
                "height": height,
                "quality_key": "high",
            })
    else:
        dbg.warn("videoHigh پیدا نشد")

    # videoLow
    low_m = re.search(r'var\s+videoLow\s*=\s*"([^"]+)"', html)
    if low_m:
        url = low_m.group(1).strip()
        dbg.ok(f"videoLow پیدا شد: `{url[:80]}`")

        if not _is_valid_cdn_url(url):
            dbg.err(f"videoLow URL نامعتبر: host={urlparse(url).hostname}")
        elif url in seen_urls:
            dbg.warn("videoLow تکراری")
        else:
            seen_urls.add(url)
            low_title_m = re.search(r'var\s+videoLowTitle\s*=\s*"([^"]+)"', html)
            label_text = low_title_m.group(1) if low_title_m else "Low"
            height = _parse_height(label_text) or 360
            dbg.ok(f"videoLow اضافه شد: {label_text}, {height}p")
            qualities.append({
                "label": f"📺 {label_text} (Low Quality)",
                "url": url,
                "method": "direct",
                "height": height,
                "quality_key": "low",
            })
    else:
        dbg.warn("videoLow پیدا نشد")


def _extract_video_tags(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    # تعداد video tags
    video_tags = re.findall(r"<video[^>]*>", html, re.I)
    dbg.info(f"تعداد <video> tags: {len(video_tags)}")
    for i, tag in enumerate(video_tags):
        dbg.info(f"  video[{i}]: `{tag[:150]}`")

    # source tags
    source_tags = re.findall(r"<source[^>]*>", html, re.I)
    dbg.info(f"تعداد <source> tags: {len(source_tags)}")
    for i, tag in enumerate(source_tags):
        dbg.info(f"  source[{i}]: `{tag[:150]}`")

    # <video src="...">
    video_src_m = re.search(
        r"<video[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I
    )
    if video_src_m:
        url = html_lib.unescape(video_src_m.group(1).strip())
        dbg.ok(f"video src پیدا شد: `{url[:80]}`")
        if url not in seen_urls and _is_valid_cdn_url(url):
            seen_urls.add(url)
            is_high = "_high" in url
            height = 720 if is_high else 360
            qualities.append({
                "label": f"📺 {'High' if is_high else 'Low'} (Video Tag)",
                "url": url,
                "method": "direct",
                "height": height,
                "quality_key": "high" if is_high else "low",
            })
            dbg.ok("اضافه شد از video tag")
        else:
            dbg.warn(f"video src رد شد (تکراری یا CDN نامعتبر)")
    else:
        dbg.warn("video src با mp4 پیدا نشد")

        # بررسی video src بدون .mp4
        any_video_src = re.search(r"<video[^>]+src=[\"']([^\"']+)[\"']", html, re.I)
        if any_video_src:
            dbg.info(f"video src (non-mp4): `{any_video_src.group(1)[:100]}`")

    # <source src="...">
    found_sources = 0
    for m in re.finditer(r"<source[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I):
        url = html_lib.unescape(m.group(1).strip())
        dbg.ok(f"source mp4 پیدا شد: `{url[:80]}`")
        found_sources += 1
        if url in seen_urls or not _is_valid_cdn_url(url):
            dbg.warn(f"source رد شد: تکراری={url in seen_urls}, cdn_valid={_is_valid_cdn_url(url)}")
            continue
        seen_urls.add(url)
        is_high = "_high" in url
        height = 720 if is_high else 360
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} (Source Tag)",
            "url": url,
            "method": "direct",
            "height": height,
            "quality_key": "high" if is_high else "low",
        })
        dbg.ok("اضافه شد از source tag")

    if found_sources == 0:
        dbg.warn("هیچ source tag با mp4 پیدا نشد")

        # بررسی source بدون mp4
        any_sources = re.findall(r"<source[^>]+src=[\"']([^\"']+)[\"']", html, re.I)
        for src in any_sources:
            dbg.info(f"source (non-mp4): `{src[:100]}`")


def _extract_direct_mp4(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    mp4_pattern = re.compile(
        r'(https?://[^\s"\'<>]*nosofiles\.com/[^\s"\'<>]+\.mp4[^\s"\'<>]*)'
    )

    matches = mp4_pattern.findall(html)
    dbg.info(f"MP4 URLs مستقیم (nosofiles): {len(matches)}")

    for url in matches:
        url = url.strip()
        dbg.info(f"  found: `{url[:100]}`")

        if "/trailer.mp4" in url or "preview" in url:
            dbg.warn(f"  → رد شد (trailer/preview)")
            continue
        if url in seen_urls:
            dbg.warn(f"  → رد شد (تکراری)")
            continue

        seen_urls.add(url)
        is_high = "_high" in url
        height = 720 if is_high else 360
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} (Direct)",
            "url": url,
            "method": "direct",
            "height": height,
            "quality_key": "high" if is_high else "low",
        })
        dbg.ok(f"  → اضافه شد")


def _extract_any_mp4(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    """جستجوی عمومی هر URL با پسوند .mp4 (بدون محدودیت CDN)."""
    mp4_pattern = re.compile(
        r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)'
    )

    matches = mp4_pattern.findall(html)
    dbg.info(f"تمام MP4 URLs (عمومی): {len(matches)}")

    for url in matches:
        url = url.strip()
        host = urlparse(url).hostname or "unknown"
        dbg.info(f"  mp4: host={host} → `{url[:120]}`")

        if url in seen_urls:
            continue
        if "/trailer.mp4" in url or "preview" in url:
            continue

        seen_urls.add(url)
        is_high = "_high" in url
        height = 720 if is_high else 360
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} ({host})",
            "url": url,
            "method": "direct",
            "height": height,
            "quality_key": f"{'high' if is_high else 'low'}_{host}",
        })
        dbg.ok(f"  → اضافه شد (host جدید: {host})")


def _extract_json_sources(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    """جستجوی JSON objects حاوی video URL."""
    # الگوی 1: sources: [{src: "..."}]
    sources_m = re.findall(
        r'sources\s*:\s*\[([^\]]+)\]', html
    )
    if sources_m:
        dbg.info(f"JS sources arrays: {len(sources_m)}")
        for block in sources_m:
            dbg.info(f"  sources block: `{block[:200]}`")
            urls = re.findall(r'src["\']?\s*:\s*["\']([^"\']+)["\']', block)
            for url in urls:
                dbg.info(f"    src: `{url[:100]}`")
                if url not in seen_urls and url.endswith(".mp4"):
                    seen_urls.add(url)
                    qualities.append({
                        "label": "📺 From JS sources",
                        "url": url,
                        "method": "direct",
                        "height": 480,
                        "quality_key": f"js_{url[-20:]}",
                    })
                    dbg.ok(f"    → اضافه شد")

    # الگوی 2: file: "..."
    file_m = re.findall(r'file\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']', html)
    if file_m:
        dbg.info(f"JS file vars: {len(file_m)}")
        for url in file_m:
            dbg.info(f"  file: `{url[:100]}`")
            if url not in seen_urls:
                seen_urls.add(url)
                qualities.append({
                    "label": "📺 From JS file",
                    "url": url,
                    "method": "direct",
                    "height": 480,
                    "quality_key": f"file_{url[-20:]}",
                })
                dbg.ok(f"  → اضافه شد")

    # الگوی 3: toStore object
    ts_m = re.search(r"const\s+toStore\s*=\s*(\{[^;]+\})", html)
    if ts_m:
        dbg.info(f"toStore object پیدا شد")
        try:
            data = json.loads(ts_m.group(1))
            dbg.info(f"toStore keys: {list(data.keys())}")
            for key, val in data.items():
                if isinstance(val, str) and (".mp4" in val or "http" in val):
                    dbg.info(f"  toStore[{key}] = `{val[:100]}`")
        except (json.JSONDecodeError, ValueError) as e:
            dbg.warn(f"toStore parse error: {e}")
            dbg.info(f"toStore raw: `{ts_m.group(1)[:300]}`")
    else:
        dbg.warn("toStore object پیدا نشد")

    # الگوی 4: هر JSON-like object با video/mp4
    json_blocks = re.findall(r'\{[^{}]*(?:mp4|video|src|file|url)[^{}]*\}', html, re.I)
    if json_blocks:
        dbg.info(f"JSON-like blocks با video/mp4: {len(json_blocks)}")
        for block in json_blocks[:5]:
            dbg.info(f"  block: `{block[:200]}`")


def _parse_height(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"(\d{3,4})p?", text)
    return int(m.group(1)) if m else None


def _extract_video_info(html: str, dbg: DebugLog) -> dict:
    info = {}

    ts_m = re.search(r"const\s+toStore\s*=\s*(\{[^;]+\})", html)
    if ts_m:
        try:
            data = json.loads(ts_m.group(1))
            info["views"] = data.get("views", 0)
            info["duration"] = data.get("length", "")
            info["likes"] = data.get("likes", "")
            info["thumbnail"] = data.get("thumbnail", "")
            info["post_id"] = data.get("postId", 0)
            dbg.ok(f"Video info: views={info.get('views')}, duration={info.get('duration')}")
        except (json.JSONDecodeError, ValueError) as e:
            dbg.warn(f"Video info parse error: {e}")

    if "thumbnail" not in info:
        poster_m = re.search(r"poster=[\"']([^\"']+)[\"']", html)
        if poster_m:
            info["thumbnail"] = poster_m.group(1)
            dbg.info(f"Poster: `{info['thumbnail'][:80]}`")

    return info


# ─── HTML Structure Debug ──────────────────────────────────


def _debug_html_structure(html: str, dbg: DebugLog) -> None:
    """آنالیز ساختار HTML برای فهمیدن مشکل."""
    dbg.step("── آنالیز ساختار HTML ──")

    # تعداد script tags
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.I | re.S)
    dbg.info(f"تعداد <script> tags: {len(scripts)}")

    # بررسی script های حاوی video
    for i, script in enumerate(scripts):
        if any(kw in script.lower() for kw in ["video", "mp4", "source", "player", "cdn", "noso"]):
            preview = script.replace("\n", " ").strip()[:300]
            dbg.info(f"  script[{i}] (video-related): `{preview}`")

    # iframe ها
    iframes = re.findall(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", html, re.I)
    if iframes:
        dbg.info(f"iframes: {len(iframes)}")
        for src in iframes:
            dbg.info(f"  iframe: `{src[:100]}`")

    # embed/object tags
    embeds = re.findall(r"<(?:embed|object)[^>]+(?:src|data)=[\"']([^\"']+)[\"']", html, re.I)
    if embeds:
        dbg.info(f"embed/object: {len(embeds)}")
        for src in embeds:
            dbg.info(f"  embed: `{src[:100]}`")

    # data attributes با URL
    data_attrs = re.findall(r'data-(?:src|url|video|file)\s*=\s*["\']([^"\']+)["\']', html, re.I)
    if data_attrs:
        dbg.info(f"data-* attributes: {len(data_attrs)}")
        for val in data_attrs:
            dbg.info(f"  data-*: `{val[:100]}`")

    # تمام hostnames در HTML
    all_hosts = set(re.findall(r'https?://([a-zA-Z0-9.-]+)', html))
    dbg.info(f"تمام hostnames در HTML ({len(all_hosts)}):")
    for host in sorted(all_hosts):
        marker = " ← CDN" if host in _CDN_HOSTS else ""
        marker += " ← ALLOWED" if host in _ALLOWED_HOSTS else ""
        dbg.info(f"  {host}{marker}")


# ─── Main extraction ───────────────────────────────────────


async def extract_xanimu_qualities(
    url: str,
    debug_callback: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict, DebugLog]:
    """
    استخراج کیفیت‌های موجود (نسخه دیباگ).

    Returns:
        (qualities, title, info, debug_log)
    """
    dbg = DebugLog()
    dbg.step(f"شروع پردازش URL: `{url}`")

    # بررسی URL
    parsed = urlparse(url)
    dbg.info(f"Host: {parsed.hostname}")
    dbg.info(f"Path: {parsed.path}")
    dbg.info(f"Scheme: {parsed.scheme}")

    if not is_xanimu_url(url):
        dbg.err(f"URL مجاز نیست! host={parsed.hostname} not in {_ALLOWED_HOSTS}")
        if debug_callback:
            await debug_callback(dbg.build_short())
        return [], "Invalid URL", {}, dbg

    dbg.ok("URL معتبر است")

    # ارسال وضعیت به تلگرام
    if debug_callback:
        await debug_callback("🔄 **در حال دریافت صفحه...**")

    # دریافت HTML
    dbg.step("── دریافت HTML با curl ──")
    html, status = await _fetch_with_curl(url, dbg)

    if not html:
        dbg.err(f"دریافت HTML ناموفق (status={status})")
        if debug_callback:
            await debug_callback(dbg.build_short())
        return [], "Failed to fetch page", {}, dbg

    # بررسی Cloudflare
    cf_indicators = ["Just a moment", "cf-browser-verification", "challenge-platform"]
    cf_found = [ind for ind in cf_indicators if ind in html]
    if cf_found or len(html) < 2000:
        dbg.err(f"Cloudflare block! indicators={cf_found}, html_size={len(html)}")
        if debug_callback:
            await debug_callback(dbg.build_short())
        return [], "Cloudflare blocked", {}, dbg

    dbg.ok("Cloudflare bypass موفق")

    # آنالیز ساختار HTML
    _debug_html_structure(html, dbg)

    # ارسال وضعیت
    if debug_callback:
        await debug_callback("🔄 **در حال استخراج لینک‌ها...**")

    # استخراج
    qualities, title = _extract_from_html(html, url, dbg)
    info = _extract_video_info(html, dbg)

    # حذف تکراری
    unique = {}
    for q in qualities:
        key = q.get("quality_key", q.get("url"))
        if key not in unique:
            unique[key] = q
    qualities = sorted(unique.values(), key=lambda q: q.get("height", 0), reverse=True)

    dbg.step("── نتیجه نهایی ──")
    if qualities:
        dbg.ok(f"✅ {len(qualities)} کیفیت پیدا شد برای: {title[:60]}")
    else:
        dbg.err("❌ هیچ کیفیتی پیدا نشد!")
        dbg.err("احتمالات:")
        dbg.err("  1. ساختار HTML سایت تغییر کرده")
        dbg.err("  2. CDN host جدید اضافه شده")
        dbg.err("  3. ویدیو حذف شده یا محدود شده")
        dbg.err("  4. لینک‌ها با JS بارگذاری میشن (نه در HTML اولیه)")

    # ارسال گزارش دیباگ به تلگرام
    if debug_callback:
        await debug_callback(dbg.build_short())

    return qualities, title, info, dbg


# ─── Download ───────────────────────────────────────────────


async def download_xanimu_video(
    url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[bool, str, int]:
    if not is_xanimu_url(url):
        return False, "URL not allowed", 0

    if not video_url:
        return False, "Empty video URL", 0

    # دانلود مستقیم
    success, error, size = await _download_direct(
        video_url, url, filepath, progress_cb,
    )
    if success:
        return True, "", size

    # Fallback: curl
    logger.info("Direct download failed: %s, trying curl", error)
    return await _download_with_curl(video_url, url, filepath, progress_cb)


async def _download_direct(
    video_url: str,
    referer: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback],
) -> Tuple[bool, str, int]:
    headers = {
        **_DEFAULT_HEADERS,
        "Referer": referer,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with aiohttp.ClientSession(
                timeout=timeout, headers=headers
            ) as session:
                async with session.get(video_url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if 400 <= resp.status < 500:
                            return False, error, 0
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(RETRY_DELAY * attempt)
                            continue
                        return False, error, 0

                    content_length = int(resp.headers.get("Content-Length", 0))
                    if content_length > MAX_DOWNLOAD_SIZE:
                        return False, f"Too large: {_format_size(content_length)}", 0

                    total_mb = content_length / 1024 / 1024 if content_length else 0
                    downloaded = 0
                    start_time = time.time()
                    last_update = 0.0

                    async with aiofiles.open(filepath, "wb") as f:
                        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                            await f.write(chunk)
                            downloaded += len(chunk)

                            if downloaded > MAX_DOWNLOAD_SIZE:
                                _cleanup_file(filepath)
                                return False, "Exceeded size limit", 0

                            now = time.time()
                            if progress_cb and now - last_update >= PROGRESS_INTERVAL:
                                last_update = now
                                await _report_progress(
                                    progress_cb, downloaded, content_length,
                                    total_mb, start_time,
                                )

            size = os.path.getsize(filepath)
            if size < MIN_FILE_SIZE:
                _cleanup_file(filepath)
                return False, f"Too small ({size} bytes)", 0

            return True, "", size

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            logger.warning("Download attempt %d/%d: %s", attempt, MAX_RETRIES, e)
            _cleanup_file(filepath)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    return False, f"Failed after {MAX_RETRIES} attempts", 0


async def _download_with_curl(
    video_url: str,
    referer: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback],
) -> Tuple[bool, str, int]:
    if not _check_curl():
        return False, "curl not found", 0

    cmd = [
        "curl", "-L",
        "-o", filepath,
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", f"Referer: {referer}",
        "-H", "Accept: */*",
        "--compressed",
        "--max-time", "3600",
        "--retry", str(MAX_RETRIES),
        "--progress-bar",
        video_url,
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        if progress_cb:
            await progress_cb("📥 **Downloading with curl...**")

        _, stderr_data = await asyncio.wait_for(
            process.communicate(), timeout=3600,
        )

        if process.returncode == 0 and os.path.exists(filepath):
            size = os.path.getsize(filepath)
            if size < MIN_FILE_SIZE:
                _cleanup_file(filepath)
                return False, f"Too small ({size} bytes)", 0
            return True, "", size

        error = stderr_data.decode(errors="replace").strip()[:200]
        _cleanup_file(filepath)
        return False, f"curl failed: {error}", 0

    except asyncio.TimeoutError:
        _cleanup_file(filepath)
        return False, "curl timeout", 0
    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        _cleanup_file(filepath)
        return False, str(e)[:200], 0


async def _report_progress(
    progress_cb: ProgressCallback,
    downloaded: int,
    content_length: int,
    total_mb: float,
    start_time: float,
) -> None:
    elapsed = time.time() - start_time
    speed = downloaded / elapsed if elapsed > 0 else 0
    dl_mb = downloaded / 1024 / 1024
    speed_kb = min(speed / 1024, MAX_SPEED_DISPLAY)

    if content_length > 0:
        pct = downloaded / content_length * 100
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        eta_secs = int((content_length - downloaded) / speed) if speed > 0 else 0
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
            f"💾 {dl_mb:.1f} MB  •  ⚡ {speed_kb:.0f} KB/s"
        )


# ─── Wrapper ────────────────────────────────────────────────


async def download_xanimu_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    return await download_xanimu_video(url, video_url, filepath, progress_cb)
