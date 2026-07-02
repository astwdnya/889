"""
xanimu_handler.py (Cloudflare Bypass Edition)
──────────────────────────────────────────────
با curl_cffi برای bypass واقعی Cloudflare
fallback به cloudscraper و playwright

pip install curl_cffi cloudscraper
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

    def build_short(self, max_entries: int = 30) -> str:
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


# ─── HTTP Fetchers (Cloudflare Bypass) ──────────────────────


async def _fetch_with_curl_cffi(url: str, dbg: DebugLog) -> Tuple[Optional[str], int]:
    """
    روش 1: curl_cffi - شبیه‌سازی TLS fingerprint Chrome واقعی.
    این بهترین روش bypass Cloudflare هست.
    """
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        dbg.warn("curl_cffi نصب نیست: `pip install curl_cffi`")
        return None, 0

    dbg.step("تلاش با curl_cffi (Chrome impersonate)...")

    # لیست browser fingerprints برای امتحان
    impersonates = ["chrome124", "chrome120", "chrome110", "chrome107"]

    for browser in impersonates:
        dbg.info(f"امتحان fingerprint: {browser}")
        try:
            async with AsyncSession(impersonate=browser) as session:
                resp = await asyncio.wait_for(
                    session.get(
                        url,
                        headers={
                            "Accept-Language": "en-US,en;q=0.9",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        },
                        allow_redirects=True,
                    ),
                    timeout=CURL_TIMEOUT,
                )

                dbg.info(f"curl_cffi [{browser}] status: {resp.status_code}")
                dbg.info(f"curl_cffi [{browser}] size: {len(resp.text)} chars")

                if resp.status_code == 200 and len(resp.text) > 2000:
                    if "Just a moment" not in resp.text:
                        dbg.ok(f"curl_cffi [{browser}] موفق!")
                        return resp.text, 200
                    else:
                        dbg.warn(f"curl_cffi [{browser}] هنوز Cloudflare challenge")
                elif resp.status_code == 403:
                    dbg.warn(f"curl_cffi [{browser}] → 403")
                else:
                    dbg.warn(f"curl_cffi [{browser}] → {resp.status_code}")

        except asyncio.TimeoutError:
            dbg.warn(f"curl_cffi [{browser}] timeout")
        except Exception as e:
            dbg.warn(f"curl_cffi [{browser}] error: {type(e).__name__}: {e}")

    dbg.err("curl_cffi: تمام fingerprints فیل شد")
    return None, 0


async def _fetch_with_cloudscraper(url: str, dbg: DebugLog) -> Tuple[Optional[str], int]:
    """
    روش 2: cloudscraper - حل JS challenge ساده Cloudflare.
    """
    try:
        import cloudscraper
    except ImportError:
        dbg.warn("cloudscraper نصب نیست: `pip install cloudscraper`")
        return None, 0

    dbg.step("تلاش با cloudscraper...")

    try:
        # cloudscraper sync هست، توی thread اجرا می‌کنیم
        def _do_request():
            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False},
            )
            resp = scraper.get(url, timeout=CURL_TIMEOUT)
            return resp.text, resp.status_code

        loop = asyncio.get_event_loop()
        html, status = await asyncio.wait_for(
            loop.run_in_executor(None, _do_request),
            timeout=CURL_TIMEOUT + 15,
        )

        dbg.info(f"cloudscraper status: {status}")
        dbg.info(f"cloudscraper size: {len(html)} chars")

        if status == 200 and len(html) > 2000 and "Just a moment" not in html:
            dbg.ok("cloudscraper موفق!")
            return html, 200

        dbg.warn(f"cloudscraper ناموفق: status={status}, cf={'Yes' if 'Just a moment' in html else 'No'}")

    except asyncio.TimeoutError:
        dbg.warn("cloudscraper timeout")
    except Exception as e:
        dbg.warn(f"cloudscraper error: {type(e).__name__}: {e}")

    return None, 0


async def _fetch_with_playwright(url: str, dbg: DebugLog) -> Tuple[Optional[str], int]:
    """
    روش 3: Playwright - مرورگر واقعی headless.
    سنگین‌ترین ولی مطمئن‌ترین.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        dbg.warn("playwright نصب نیست: `pip install playwright && playwright install chromium`")
        return None, 0

    dbg.step("تلاش با Playwright (headless browser)...")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1920, "height": 1080},
            )
            page = await context.new_page()

            dbg.info("مرورگر باز شد، در حال بارگذاری صفحه...")

            resp = await page.goto(url, wait_until="networkidle", timeout=45000)
            status = resp.status if resp else 0

            # صبر اضافی برای JS
            await page.wait_for_timeout(3000)

            html = await page.content()

            dbg.info(f"Playwright status: {status}")
            dbg.info(f"Playwright size: {len(html)} chars")
            dbg.info(f"Playwright title: {await page.title()}")

            await browser.close()

            if len(html) > 2000 and "Just a moment" not in html:
                dbg.ok("Playwright موفق!")
                return html, 200

            dbg.warn("Playwright: هنوز Cloudflare challenge")

    except asyncio.TimeoutError:
        dbg.warn("Playwright timeout")
    except Exception as e:
        dbg.warn(f"Playwright error: {type(e).__name__}: {e}")

    return None, 0


async def _fetch_with_system_curl(url: str, dbg: DebugLog) -> Tuple[Optional[str], int]:
    """
    روش 4: curl سیستم با TLS options اضافی.
    """
    if not _check_curl():
        dbg.warn("curl سیستم پیدا نشد")
        return None, 0

    dbg.step("تلاش با curl سیستم (TLS tweaks)...")

    # تنظیمات مختلف TLS
    tls_configs = [
        # Config 1: cipher list مشابه Chrome
        [
            "--ciphers",
            "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:"
            "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256",
            "--tlsv1.2",
        ],
        # Config 2: ساده
        ["--tlsv1.3"],
        # Config 3: بدون تنظیم خاص
        [],
    ]

    for i, tls_opts in enumerate(tls_configs):
        dbg.info(f"curl config {i+1}/{len(tls_configs)}")

        cmd = [
            "curl", "-s",
            "-w", "\n__HTTP_CODE__%{http_code}",
            "-H", f"User-Agent: {_USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", "sec-ch-ua: \"Chromium\";v=\"126\", \"Google Chrome\";v=\"126\"",
            "-H", "sec-ch-ua-mobile: ?0",
            "-H", "sec-ch-ua-platform: \"Windows\"",
            "-H", "Sec-Fetch-Dest: document",
            "-H", "Sec-Fetch-Mode: navigate",
            "-H", "Sec-Fetch-Site: none",
            "-H", "Sec-Fetch-User: ?1",
            "-H", "Upgrade-Insecure-Requests: 1",
            "-L",
            "--compressed",
            "--max-time", str(CURL_TIMEOUT),
            *tls_opts,
            url,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=CURL_TIMEOUT + 10,
            )

            output = stdout.decode(errors="replace")
            code_marker = "__HTTP_CODE__"
            if code_marker in output:
                parts = output.rsplit(code_marker, 1)
                html = parts[0]
                try:
                    status = int(parts[1].strip())
                except (ValueError, IndexError):
                    status = 0
            else:
                html = output
                status = 200 if process.returncode == 0 else 0

            dbg.info(f"curl config {i+1}: status={status}, size={len(html)}")

            if status == 200 and len(html) > 2000 and "Just a moment" not in html:
                dbg.ok(f"curl config {i+1} موفق!")
                return html, 200

        except asyncio.TimeoutError:
            dbg.warn(f"curl config {i+1} timeout")
        except Exception as e:
            dbg.warn(f"curl config {i+1} error: {e}")

    dbg.err("curl سیستم: تمام configs فیل شد")
    return None, 0


async def _fetch_page(url: str, dbg: DebugLog) -> Tuple[Optional[str], int]:
    """
    تلاش با تمام روش‌ها به ترتیب اولویت.
    """
    dbg.step("══ شروع دریافت صفحه ══")

    # روش 1: curl_cffi (سریع‌ترین و بهترین)
    html, status = await _fetch_with_curl_cffi(url, dbg)
    if html:
        return html, status

    # روش 2: cloudscraper
    html, status = await _fetch_with_cloudscraper(url, dbg)
    if html:
        return html, status

    # روش 3: curl سیستم با TLS tweaks
    html, status = await _fetch_with_system_curl(url, dbg)
    if html:
        return html, status

    # روش 4: Playwright (آخرین چاره)
    html, status = await _fetch_with_playwright(url, dbg)
    if html:
        return html, status

    dbg.err("══ تمام روش‌ها فیل شد! ══")
    dbg.err("پیشنهاد: `pip install curl_cffi cloudscraper`")
    return None, 0


# ─── Extraction (بدون تغییر از نسخه قبلی) ──────────────────


def _extract_from_html(html: str, page_url: str, dbg: DebugLog) -> Tuple[List[dict], str]:
    title = _extract_title(html, dbg)
    qualities: List[dict] = []
    seen_urls: set = set()

    dbg.step("── شروع استخراج کیفیت‌ها ──")

    _extract_js_vars(html, qualities, seen_urls, dbg)
    _extract_video_tags(html, qualities, seen_urls, dbg)
    _extract_direct_mp4(html, qualities, seen_urls, dbg)
    _extract_any_mp4(html, qualities, seen_urls, dbg)
    _extract_json_sources(html, qualities, seen_urls, dbg)

    qualities.sort(key=lambda q: q.get("height", 0), reverse=True)

    dbg.info(f"مجموع کیفیت‌های پیدا شده: {len(qualities)}")
    for i, q in enumerate(qualities):
        dbg.ok(f"  کیفیت {i+1}: {q['label']} → `{q['url'][:80]}...`")

    return qualities, title


def _extract_title(html: str, dbg: DebugLog) -> str:
    dbg.step("استخراج عنوان...")

    ts_m = re.search(r'"title"\s*:\s*"([^"]+)"', html)
    if ts_m:
        title = ts_m.group(1).strip()
        if len(title) > 3:
            dbg.ok(f"عنوان از JSON: `{title[:60]}`")
            return html_lib.unescape(title)

    t_m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if t_m:
        title = t_m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*XAnimu\.com\s*$", "", title, flags=re.I).strip()
        if title:
            dbg.ok(f"عنوان از <title>: `{title[:60]}`")
            return html_lib.unescape(title)

    og_m = re.search(r'property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.I)
    if og_m:
        title = html_lib.unescape(og_m.group(1).strip())
        dbg.ok(f"عنوان از og:title: `{title[:60]}`")
        return title

    h1_m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    if h1_m:
        title = html_lib.unescape(h1_m.group(1).strip())
        dbg.ok(f"عنوان از h1: `{title[:60]}`")
        return title

    dbg.warn("عنوان پیدا نشد")
    return "Untitled"


def _extract_js_vars(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    all_vars = re.findall(r'var\s+(\w*[Vv]ideo\w*)\s*=\s*"([^"]*)"', html)
    if all_vars:
        dbg.info(f"JS video vars: {len(all_vars)}")
        for name, val in all_vars:
            dbg.info(f"  var {name} = `{val[:100]}`")
    else:
        dbg.warn("هیچ JS video var پیدا نشد")
        any_vars = re.findall(r'var\s+(\w+)\s*=\s*"(https?://[^"]+)"', html)
        if any_vars:
            dbg.info(f"سایر JS vars با URL: {len(any_vars)}")
            for name, val in any_vars[:10]:
                dbg.info(f"  var {name} = `{val[:100]}`")

        let_vars = re.findall(r'(?:let|const)\s+(\w*[Vv]ideo\w*)\s*=\s*["\']([^"\']*)["\']', html)
        if let_vars:
            dbg.info(f"let/const video vars: {len(let_vars)}")
            for name, val in let_vars:
                dbg.info(f"  {name} = `{val[:100]}`")

    # videoHigh
    high_m = re.search(r'var\s+videoHigh\s*=\s*"([^"]+)"', html)
    if high_m:
        url = high_m.group(1).strip()
        dbg.ok(f"videoHigh: `{url[:80]}`")
        if not _is_valid_cdn_url(url):
            dbg.err(f"videoHigh CDN نامعتبر: {urlparse(url).hostname}")
        elif url in seen_urls:
            dbg.warn("videoHigh تکراری")
        else:
            seen_urls.add(url)
            high_title_m = re.search(r'var\s+videoHighTitle\s*=\s*"([^"]+)"', html)
            label_text = high_title_m.group(1) if high_title_m else "High"
            height = _parse_height(label_text) or 720
            qualities.append({
                "label": f"📺 {label_text} (High Quality)",
                "url": url, "method": "direct",
                "height": height, "quality_key": "high",
            })
    else:
        dbg.warn("videoHigh پیدا نشد")

    # videoLow
    low_m = re.search(r'var\s+videoLow\s*=\s*"([^"]+)"', html)
    if low_m:
        url = low_m.group(1).strip()
        dbg.ok(f"videoLow: `{url[:80]}`")
        if not _is_valid_cdn_url(url):
            dbg.err(f"videoLow CDN نامعتبر: {urlparse(url).hostname}")
        elif url in seen_urls:
            dbg.warn("videoLow تکراری")
        else:
            seen_urls.add(url)
            low_title_m = re.search(r'var\s+videoLowTitle\s*=\s*"([^"]+)"', html)
            label_text = low_title_m.group(1) if low_title_m else "Low"
            height = _parse_height(label_text) or 360
            qualities.append({
                "label": f"📺 {label_text} (Low Quality)",
                "url": url, "method": "direct",
                "height": height, "quality_key": "low",
            })
    else:
        dbg.warn("videoLow پیدا نشد")


def _extract_video_tags(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    video_tags = re.findall(r"<video[^>]*>", html, re.I)
    dbg.info(f"<video> tags: {len(video_tags)}")

    source_tags = re.findall(r"<source[^>]*>", html, re.I)
    dbg.info(f"<source> tags: {len(source_tags)}")

    video_src_m = re.search(
        r"<video[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I
    )
    if video_src_m:
        url = html_lib.unescape(video_src_m.group(1).strip())
        dbg.ok(f"video src: `{url[:80]}`")
        if url not in seen_urls and _is_valid_cdn_url(url):
            seen_urls.add(url)
            is_high = "_high" in url
            qualities.append({
                "label": f"📺 {'High' if is_high else 'Low'} (Video Tag)",
                "url": url, "method": "direct",
                "height": 720 if is_high else 360,
                "quality_key": "high" if is_high else "low",
            })
    else:
        dbg.warn("video src با mp4 پیدا نشد")

    for m in re.finditer(r"<source[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I):
        url = html_lib.unescape(m.group(1).strip())
        if url in seen_urls or not _is_valid_cdn_url(url):
            continue
        seen_urls.add(url)
        is_high = "_high" in url
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} (Source Tag)",
            "url": url, "method": "direct",
            "height": 720 if is_high else 360,
            "quality_key": "high" if is_high else "low",
        })


def _extract_direct_mp4(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    mp4_pattern = re.compile(
        r'(https?://[^\s"\'<>]*nosofiles\.com/[^\s"\'<>]+\.mp4[^\s"\'<>]*)'
    )
    matches = mp4_pattern.findall(html)
    dbg.info(f"MP4 URLs (nosofiles): {len(matches)}")

    for url in matches:
        url = url.strip()
        if "/trailer.mp4" in url or "preview" in url:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        is_high = "_high" in url
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} (Direct)",
            "url": url, "method": "direct",
            "height": 720 if is_high else 360,
            "quality_key": "high" if is_high else "low",
        })


def _extract_any_mp4(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    mp4_pattern = re.compile(r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)')
    matches = mp4_pattern.findall(html)
    dbg.info(f"تمام MP4 URLs: {len(matches)}")

    for url in matches:
        url = url.strip()
        host = urlparse(url).hostname or "unknown"
        dbg.info(f"  mp4: host={host} → `{url[:120]}`")
        if url in seen_urls or "/trailer.mp4" in url or "preview" in url:
            continue
        seen_urls.add(url)
        is_high = "_high" in url
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} ({host})",
            "url": url, "method": "direct",
            "height": 720 if is_high else 360,
            "quality_key": f"{'high' if is_high else 'low'}_{host}",
        })
        dbg.ok(f"  → اضافه شد ({host})")


def _extract_json_sources(
    html: str, qualities: List[dict], seen_urls: set, dbg: DebugLog
) -> None:
    sources_m = re.findall(r'sources\s*:\s*\[([^\]]+)\]', html)
    if sources_m:
        dbg.info(f"JS sources arrays: {len(sources_m)}")
        for block in sources_m:
            urls = re.findall(r'src["\']?\s*:\s*["\']([^"\']+)["\']', block)
            for url in urls:
                if url not in seen_urls and ".mp4" in url:
                    seen_urls.add(url)
                    qualities.append({
                        "label": "📺 From JS sources",
                        "url": url, "method": "direct",
                        "height": 480, "quality_key": f"js_{url[-20:]}",
                    })

    file_m = re.findall(r'file\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']', html)
    if file_m:
        dbg.info(f"JS file vars: {len(file_m)}")
        for url in file_m:
            if url not in seen_urls:
                seen_urls.add(url)
                qualities.append({
                    "label": "📺 From JS file",
                    "url": url, "method": "direct",
                    "height": 480, "quality_key": f"file_{url[-20:]}",
                })

    ts_m = re.search(r"const\s+toStore\s*=\s*(\{[^;]+\})", html)
    if ts_m:
        dbg.info("toStore object پیدا شد")
        try:
            data = json.loads(ts_m.group(1))
            dbg.info(f"toStore keys: {list(data.keys())}")
            for key, val in data.items():
                if isinstance(val, str) and (".mp4" in val or "http" in val):
                    dbg.info(f"  toStore[{key}] = `{val[:100]}`")
        except (json.JSONDecodeError, ValueError) as e:
            dbg.warn(f"toStore parse error: {e}")
    else:
        dbg.warn("toStore پیدا نشد")


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
        except (json.JSONDecodeError, ValueError):
            pass

    if "thumbnail" not in info:
        poster_m = re.search(r"poster=[\"']([^\"']+)[\"']", html)
        if poster_m:
            info["thumbnail"] = poster_m.group(1)

    return info


def _debug_html_structure(html: str, dbg: DebugLog) -> None:
    dbg.step("── آنالیز ساختار HTML ──")

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.I | re.S)
    dbg.info(f"<script> tags: {len(scripts)}")
    for i, script in enumerate(scripts):
        if any(kw in script.lower() for kw in ["video", "mp4", "source", "player", "cdn", "noso"]):
            preview = script.replace("\n", " ").strip()[:300]
            dbg.info(f"  script[{i}] (video): `{preview}`")

    iframes = re.findall(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", html, re.I)
    if iframes:
        dbg.info(f"iframes: {len(iframes)}")
        for src in iframes:
            dbg.info(f"  iframe: `{src[:100]}`")

    data_attrs = re.findall(r'data-(?:src|url|video|file)\s*=\s*["\']([^"\']+)["\']', html, re.I)
    if data_attrs:
        dbg.info(f"data-* attrs: {len(data_attrs)}")
        for val in data_attrs:
            dbg.info(f"  data-*: `{val[:100]}`")

    all_hosts = set(re.findall(r'https?://([a-zA-Z0-9.-]+)', html))
    dbg.info(f"Hostnames ({len(all_hosts)}):")
    for host in sorted(all_hosts):
        marker = ""
        if host in _CDN_HOSTS:
            marker = " ← CDN"
        if host in _ALLOWED_HOSTS:
            marker = " ← ALLOWED"
        dbg.info(f"  {host}{marker}")


# ─── Main extraction ───────────────────────────────────────


async def extract_xanimu_qualities(
    url: str,
    debug_callback: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict, DebugLog]:
    dbg = DebugLog()
    dbg.step(f"URL: `{url}`")

    parsed = urlparse(url)
    dbg.info(f"Host: {parsed.hostname}, Path: {parsed.path}")

    if not is_xanimu_url(url):
        dbg.err(f"URL نامعتبر: {parsed.hostname}")
        if debug_callback:
            await debug_callback(dbg.build_short())
        return [], "Invalid URL", {}, dbg

    dbg.ok("URL معتبر")

    if debug_callback:
        await debug_callback("🔄 **در حال bypass کردن Cloudflare...**")

    # دریافت HTML با تمام روش‌ها
    html, status = await _fetch_page(url, dbg)

    if not html:
        dbg.err(f"دریافت HTML ناموفق (status={status})")
        if debug_callback:
            await debug_callback(dbg.build_short())
        return [], "Failed to fetch page", {}, dbg

    cf_indicators = ["Just a moment", "cf-browser-verification", "challenge-platform"]
    cf_found = [ind for ind in cf_indicators if ind in html]
    if cf_found or len(html) < 2000:
        dbg.err(f"Cloudflare block: {cf_found}, size={len(html)}")
        if debug_callback:
            await debug_callback(dbg.build_short())
        return [], "Cloudflare blocked", {}, dbg

    dbg.ok("Cloudflare bypass موفق!")

    _debug_html_structure(html, dbg)

    if debug_callback:
        await debug_callback("🔄 **در حال استخراج لینک‌ها...**")

    qualities, title = _extract_from_html(html, url, dbg)
    info = _extract_video_info(html, dbg)

    unique = {}
    for q in qualities:
        key = q.get("quality_key", q.get("url"))
        if key not in unique:
            unique[key] = q
    qualities = sorted(unique.values(), key=lambda q: q.get("height", 0), reverse=True)

    dbg.step("── نتیجه ──")
    if qualities:
        dbg.ok(f"{len(qualities)} کیفیت پیدا شد: {title[:60]}")
    else:
        dbg.err("هیچ کیفیتی پیدا نشد!")

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

    success, error, size = await _download_direct(
        video_url, url, filepath, progress_cb,
    )
    if success:
        return True, "", size

    logger.info("Direct failed: %s, trying curl", error)
    return await _download_with_curl(video_url, url, filepath, progress_cb)


async def _download_direct(
    video_url: str,
    referer: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback],
) -> Tuple[bool, str, int]:
    headers = {**_DEFAULT_HEADERS, "Referer": referer}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
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

        _, stderr_data = await asyncio.wait_for(process.communicate(), timeout=3600)

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
            f"💾 {dl_mb:.1f}/{total_mb:.1f} MB  •  ⚡ {speed_kb:.0f} KB/s\n"
            f"📊 {pct:.1f}%  •  ⏱ ETA: {eta_m}:{eta_s:02d}"
        )
    else:
        await progress_cb(
            f"📥 **Downloading...**\n"
            f"💾 {dl_mb:.1f} MB  •  ⚡ {speed_kb:.0f} KB/s"
        )


async def download_xanimu_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
) -> Tuple[bool, str, int]:
    return await download_xanimu_video(url, video_url, filepath, progress_cb)
