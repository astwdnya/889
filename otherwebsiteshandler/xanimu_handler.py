"""
xanimu_handler.py (Playwright Edition)
───────────────────────────────────────
Cloudflare bypass واقعی با مرورگر headless

pip install playwright aiofiles aiohttp
playwright install chromium
"""

import asyncio
import html as html_lib
import json
import logging
import os
import re
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

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
MAX_RETRIES = 3
RETRY_DELAY = 2.0
MIN_FILE_SIZE = 1024
CHUNK_SIZE = 256 * 1024
PROGRESS_INTERVAL = 2.0
MAX_SPEED_DISPLAY = 99999

# صبر برای حل شدن Cloudflare challenge (ثانیه)
CF_WAIT_TIMEOUT = 45
CF_CHECK_INTERVAL = 2

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


# ─── Cookie Store ───────────────────────────────────────────


class CloudflareCookieStore:
    """
    ذخیره cf_clearance cookies برای استفاده مجدد.
    هر cookie حدود 30 دقیقه اعتبار داره.
    """

    def __init__(self):
        self._cookies: dict[str, dict] = {}  # domain -> {cookies, timestamp, user_agent}
        self._lock = asyncio.Lock()
        self._ttl = 25 * 60  # 25 دقیقه (کمتر از 30 دقیقه واقعی)

    async def get(self, domain: str) -> Optional[dict]:
        async with self._lock:
            entry = self._cookies.get(domain)
            if entry and time.time() - entry["timestamp"] < self._ttl:
                logger.debug("Cookie cache hit for %s", domain)
                return entry
            if entry:
                del self._cookies[domain]
            return None

    async def put(self, domain: str, cookies: list, user_agent: str) -> None:
        async with self._lock:
            self._cookies[domain] = {
                "cookies": cookies,
                "user_agent": user_agent,
                "timestamp": time.time(),
            }
            logger.debug("Cookie cached for %s", domain)


# Global cookie store
_cookie_store = CloudflareCookieStore()


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


# ─── Playwright: Cloudflare Bypass ──────────────────────────


async def _solve_cloudflare(
    url: str,
    dbg: DebugLog,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[Optional[str], list, str]:
    """
    باز کردن صفحه با Playwright و حل Cloudflare challenge.

    Returns:
        (html, cookies, user_agent) یا (None, [], "")
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        dbg.err("playwright نصب نیست!")
        dbg.err("اجرا کن: pip install playwright && playwright install chromium")
        return None, [], ""

    domain = urlparse(url).hostname or ""

    # چک cache
    cached = await _cookie_store.get(domain)
    if cached:
        dbg.ok(f"Cookie cache hit برای {domain}")
        # با cookie ذخیره شده HTML بگیر
        html = await _fetch_with_cookies(url, cached["cookies"], cached["user_agent"], dbg)
        if html and "Just a moment" not in html and len(html) > 2000:
            dbg.ok("صفحه با cookie cache دریافت شد")
            return html, cached["cookies"], cached["user_agent"]
        dbg.warn("Cookie cache منقضی شده، مرورگر باز میشه")

    dbg.step("باز کردن مرورگر Chromium...")
    if progress_cb:
        await progress_cb("🌐 **در حال باز کردن مرورگر برای bypass Cloudflare...**")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )

            context = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
            )

            # حذف نشانه‌های automation
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                window.chrome = {runtime: {}};
            """)

            page = await context.new_page()
            dbg.ok("مرورگر باز شد")

            # رفتن به صفحه
            dbg.step(f"بارگذاری: {url}")
            if progress_cb:
                await progress_cb("🔄 **در حال حل Cloudflare challenge...**\n⏳ ممکنه تا 30 ثانیه طول بکشه")

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                dbg.warn(f"goto initial error (ادامه میدیم): {e}")

            # صبر برای حل شدن Cloudflare
            html = ""
            solved = False
            start = time.time()

            while time.time() - start < CF_WAIT_TIMEOUT:
                await asyncio.sleep(CF_CHECK_INTERVAL)
                elapsed = time.time() - start

                try:
                    html = await page.content()
                    page_title = await page.title()
                except Exception:
                    continue

                dbg.info(f"[{elapsed:.0f}s] title='{page_title}', size={len(html)}")

                # چک کن Cloudflare حل شده
                if "Just a moment" not in html and len(html) > 3000:
                    dbg.ok(f"Cloudflare حل شد! ({elapsed:.1f}s)")
                    solved = True
                    break

                if progress_cb and int(elapsed) % 5 == 0:
                    await progress_cb(
                        f"🔄 **حل Cloudflare challenge...**\n"
                        f"⏳ {elapsed:.0f}/{CF_WAIT_TIMEOUT}s"
                    )

            if not solved:
                dbg.err(f"Cloudflare بعد از {CF_WAIT_TIMEOUT}s حل نشد")
                # آخرین تلاش: صبر بیشتر
                dbg.step("تلاش آخر: 15 ثانیه صبر اضافی...")
                await asyncio.sleep(15)
                html = await page.content()
                if "Just a moment" in html or len(html) < 3000:
                    await browser.close()
                    return None, [], ""
                dbg.ok("Cloudflare با صبر اضافی حل شد!")

            # گرفتن cookies
            cookies = await context.cookies()
            user_agent = await page.evaluate("navigator.userAgent")

            dbg.info(f"Cookies: {len(cookies)}")
            cf_cookies = [c for c in cookies if "cf_clearance" in c.get("name", "")]
            if cf_cookies:
                dbg.ok(f"cf_clearance cookie پیدا شد!")
                for c in cf_cookies:
                    dbg.info(f"  {c['name']}={c['value'][:30]}... domain={c.get('domain')}")
            else:
                dbg.warn("cf_clearance cookie پیدا نشد")
                dbg.info(f"تمام cookies: {[c['name'] for c in cookies]}")

            # ذخیره در cache
            await _cookie_store.put(domain, cookies, user_agent)

            # ذخیره cookies برای CDN هم
            for cdn_host in _CDN_HOSTS:
                cdn_cached = await _cookie_store.get(cdn_host)
                if not cdn_cached:
                    # CDN cookies رو جداگانه باید بگیریم
                    pass

            await browser.close()
            dbg.ok("مرورگر بسته شد")

            return html, cookies, user_agent

    except Exception as e:
        dbg.err(f"Playwright error: {type(e).__name__}: {e}")
        return None, [], ""


async def _fetch_with_cookies(
    url: str, cookies: list, user_agent: str, dbg: DebugLog
) -> Optional[str]:
    """دریافت HTML با cookies ذخیره شده (بدون مرورگر)."""
    try:
        jar = aiohttp.CookieJar(unsafe=True)
        timeout = ClientTimeout(total=30)

        async with aiohttp.ClientSession(
            cookie_jar=jar, timeout=timeout
        ) as session:
            # تنظیم cookies
            for cookie in cookies:
                session.cookie_jar.update_cookies(
                    {cookie["name"]: cookie["value"]},
                    response_url=aiohttp.client.URL(
                        f"https://{cookie.get('domain', '').lstrip('.')}"
                    ),
                )

            headers = {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }

            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status == 200:
                    return await resp.text()
                dbg.warn(f"Cookie fetch: HTTP {resp.status}")

    except Exception as e:
        dbg.warn(f"Cookie fetch error: {e}")

    return None


async def _solve_cdn_cloudflare(
    video_url: str,
    page_cookies: list,
    user_agent: str,
    dbg: DebugLog,
    progress_cb: Optional[ProgressCallback] = None,
) -> Tuple[list, str]:
    """
    حل Cloudflare challenge روی CDN.
    CDN هم ممکنه challenge بده، پس باید مرورگر رو بفرستیم اونجا هم.

    Returns:
        (cdn_cookies, user_agent)
    """
    cdn_host = urlparse(video_url).hostname or ""

    # اول با cookie صفحه اصلی امتحان کن
    cached = await _cookie_store.get(cdn_host)
    if cached:
        dbg.ok(f"CDN cookie cache hit: {cdn_host}")
        return cached["cookies"], cached["user_agent"]

    dbg.step(f"حل Cloudflare برای CDN: {cdn_host}")
    if progress_cb:
        await progress_cb(f"🔄 **حل Cloudflare برای CDN ({cdn_host})...**")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        dbg.err("playwright نصب نیست")
        return page_cookies, user_agent

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )

            context = await browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )

            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
            """)

            # اضافه کردن cookies صفحه اصلی
            valid_cookies = []
            for c in page_cookies:
                try:
                    cookie_data = {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain", ""),
                        "path": c.get("path", "/"),
                    }
                    if c.get("expires") and c["expires"] > 0:
                        cookie_data["expires"] = c["expires"]
                    valid_cookies.append(cookie_data)
                except (KeyError, TypeError):
                    continue

            if valid_cookies:
                await context.add_cookies(valid_cookies)

            page = await context.new_page()

            # رفتن به URL ویدیو (Cloudflare challenge میاد)
            dbg.step(f"بارگذاری CDN URL...")
            try:
                await page.goto(video_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                dbg.warn(f"CDN goto error (ادامه میدیم): {e}")

            # صبر برای حل challenge
            start = time.time()
            solved = False

            while time.time() - start < CF_WAIT_TIMEOUT:
                await asyncio.sleep(CF_CHECK_INTERVAL)
                elapsed = time.time() - start

                try:
                    content = await page.content()
                    resp_url = page.url
                except Exception:
                    continue

                dbg.info(f"CDN [{elapsed:.0f}s] url={resp_url[:80]}, size={len(content)}")

                # اگه challenge حل شد، redirect به فایل واقعی میشه
                # یا content-type تغییر می‌کنه
                if "Just a moment" not in content:
                    dbg.ok(f"CDN Cloudflare حل شد! ({elapsed:.1f}s)")
                    solved = True
                    break

                if progress_cb and int(elapsed) % 5 == 0:
                    await progress_cb(
                        f"🔄 **حل Cloudflare CDN...**\n"
                        f"⏳ {elapsed:.0f}/{CF_WAIT_TIMEOUT}s"
                    )

            if not solved:
                dbg.warn("CDN Cloudflare حل نشد، ادامه با cookies موجود")

            cookies = await context.cookies()
            cdn_ua = await page.evaluate("navigator.userAgent")

            cdn_cf = [c for c in cookies if "cf_clearance" in c.get("name", "")]
            if cdn_cf:
                dbg.ok(f"CDN cf_clearance پیدا شد!")

            await _cookie_store.put(cdn_host, cookies, cdn_ua)
            await browser.close()

            return cookies, cdn_ua

    except Exception as e:
        dbg.err(f"CDN Playwright error: {e}")
        return page_cookies, user_agent


# ─── Extraction ─────────────────────────────────────────────


def _extract_from_html(html: str, dbg: DebugLog) -> Tuple[List[dict], str]:
    title = _extract_title(html, dbg)
    qualities: List[dict] = []
    seen_urls: set = set()

    dbg.step("── استخراج کیفیت‌ها ──")

    _extract_js_vars(html, qualities, seen_urls, dbg)
    _extract_video_tags(html, qualities, seen_urls, dbg)
    _extract_direct_mp4(html, qualities, seen_urls, dbg)
    _extract_any_mp4(html, qualities, seen_urls, dbg)
    _extract_json_sources(html, qualities, seen_urls, dbg)

    qualities.sort(key=lambda q: q.get("height", 0), reverse=True)

    dbg.info(f"مجموع: {len(qualities)} کیفیت")
    for i, q in enumerate(qualities):
        dbg.ok(f"  {i+1}. {q['label']} → `{q['url'][:80]}...`")

    return qualities, title


def _extract_title(html: str, dbg: DebugLog) -> str:
    ts_m = re.search(r'"title"\s*:\s*"([^"]+)"', html)
    if ts_m:
        title = ts_m.group(1).strip()
        if len(title) > 3:
            dbg.ok(f"عنوان: `{title[:60]}`")
            return html_lib.unescape(title)

    t_m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if t_m:
        title = t_m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*XAnimu\.com\s*$", "", title, flags=re.I).strip()
        if title:
            dbg.ok(f"عنوان: `{title[:60]}`")
            return html_lib.unescape(title)

    og_m = re.search(r'property=["\']og:title["\']\s+content=["\']([^"\']+)', html, re.I)
    if og_m:
        return html_lib.unescape(og_m.group(1).strip())

    h1_m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.I)
    if h1_m:
        return html_lib.unescape(h1_m.group(1).strip())

    dbg.warn("عنوان پیدا نشد")
    return "Untitled"


def _extract_js_vars(html: str, qualities: List[dict], seen: set, dbg: DebugLog) -> None:
    all_vars = re.findall(r'var\s+(\w*[Vv]ideo\w*)\s*=\s*"([^"]*)"', html)
    if all_vars:
        dbg.info(f"JS video vars: {len(all_vars)}")
        for name, val in all_vars:
            dbg.info(f"  {name} = `{val[:100]}`")
    else:
        dbg.warn("JS video vars پیدا نشد")
        # let/const
        let_vars = re.findall(r'(?:let|const)\s+(\w*[Vv]ideo\w*)\s*=\s*["\']([^"\']*)["\']', html)
        if let_vars:
            dbg.info(f"let/const video vars: {len(let_vars)}")
            for name, val in let_vars:
                dbg.info(f"  {name} = `{val[:100]}`")

    for var_name, quality_key, default_height in [
        ("videoHigh", "high", 720),
        ("videoLow", "low", 360),
    ]:
        m = re.search(rf'var\s+{var_name}\s*=\s*"([^"]+)"', html)
        if m:
            url = m.group(1).strip()
            dbg.ok(f"{var_name}: `{url[:80]}`")
            if url not in seen:
                seen.add(url)
                title_m = re.search(rf'var\s+{var_name}Title\s*=\s*"([^"]+)"', html)
                label = title_m.group(1) if title_m else quality_key.title()
                height = _parse_height(label) or default_height
                qualities.append({
                    "label": f"📺 {label} ({quality_key.title()} Quality)",
                    "url": url, "method": "direct",
                    "height": height, "quality_key": quality_key,
                })
        else:
            dbg.warn(f"{var_name} پیدا نشد")


def _extract_video_tags(html: str, qualities: List[dict], seen: set, dbg: DebugLog) -> None:
    video_src = re.search(r"<video[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I)
    if video_src:
        url = html_lib.unescape(video_src.group(1).strip())
        dbg.ok(f"video src: `{url[:80]}`")
        if url not in seen:
            seen.add(url)
            is_high = "_high" in url
            qualities.append({
                "label": f"📺 {'High' if is_high else 'Low'} (Video Tag)",
                "url": url, "method": "direct",
                "height": 720 if is_high else 360,
                "quality_key": "high" if is_high else "low",
            })

    for m in re.finditer(r"<source[^>]+src=[\"']([^\"']+\.mp4[^\"']*)[\"']", html, re.I):
        url = html_lib.unescape(m.group(1).strip())
        if url not in seen:
            seen.add(url)
            is_high = "_high" in url
            qualities.append({
                "label": f"📺 {'High' if is_high else 'Low'} (Source)",
                "url": url, "method": "direct",
                "height": 720 if is_high else 360,
                "quality_key": "high" if is_high else "low",
            })
            dbg.ok(f"source: `{url[:80]}`")


def _extract_direct_mp4(html: str, qualities: List[dict], seen: set, dbg: DebugLog) -> None:
    pattern = re.compile(r'(https?://[^\s"\'<>]*nosofiles\.com/[^\s"\'<>]+\.mp4[^\s"\'<>]*)')
    matches = pattern.findall(html)
    dbg.info(f"nosofiles MP4: {len(matches)}")
    for url in matches:
        url = url.strip()
        if "/trailer.mp4" in url or "preview" in url or url in seen:
            continue
        seen.add(url)
        is_high = "_high" in url
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} (Direct)",
            "url": url, "method": "direct",
            "height": 720 if is_high else 360,
            "quality_key": "high" if is_high else "low",
        })
        dbg.ok(f"direct: `{url[:80]}`")


def _extract_any_mp4(html: str, qualities: List[dict], seen: set, dbg: DebugLog) -> None:
    matches = re.findall(r'(https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*)', html)
    dbg.info(f"تمام MP4: {len(matches)}")
    for url in matches:
        url = url.strip()
        host = urlparse(url).hostname or "?"
        if url in seen or "/trailer.mp4" in url or "preview" in url:
            continue
        seen.add(url)
        is_high = "_high" in url
        qualities.append({
            "label": f"📺 {'High' if is_high else 'Low'} ({host})",
            "url": url, "method": "direct",
            "height": 720 if is_high else 360,
            "quality_key": f"{'high' if is_high else 'low'}_{host}",
        })
        dbg.ok(f"mp4 ({host}): `{url[:80]}`")


def _extract_json_sources(html: str, qualities: List[dict], seen: set, dbg: DebugLog) -> None:
    for block in re.findall(r'sources\s*:\s*\[([^\]]+)\]', html):
        for url in re.findall(r'src["\']?\s*:\s*["\']([^"\']+)["\']', block):
            if url not in seen and ".mp4" in url:
                seen.add(url)
                qualities.append({
                    "label": "📺 JS sources", "url": url, "method": "direct",
                    "height": 480, "quality_key": f"js_{len(seen)}",
                })

    for url in re.findall(r'file\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']', html):
        if url not in seen:
            seen.add(url)
            qualities.append({
                "label": "📺 JS file", "url": url, "method": "direct",
                "height": 480, "quality_key": f"file_{len(seen)}",
            })

    ts_m = re.search(r"const\s+toStore\s*=\s*(\{[^;]+\})", html)
    if ts_m:
        dbg.info("toStore پیدا شد")
        try:
            data = json.loads(ts_m.group(1))
            dbg.info(f"toStore keys: {list(data.keys())}")
        except (json.JSONDecodeError, ValueError) as e:
            dbg.warn(f"toStore parse error: {e}")


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
        except (json.JSONDecodeError, ValueError):
            pass
    return info


def _debug_html_structure(html: str, dbg: DebugLog) -> None:
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.I | re.S)
    dbg.info(f"Scripts: {len(scripts)}")
    for i, s in enumerate(scripts):
        if any(kw in s.lower() for kw in ["video", "mp4", "player", "cdn", "noso"]):
            dbg.info(f"  script[{i}]: `{s.replace(chr(10), ' ')[:200]}`")

    all_hosts = set(re.findall(r'https?://([a-zA-Z0-9.-]+)', html))
    dbg.info(f"Hosts: {sorted(all_hosts)}")


# ─── Main API ──────────────────────────────────────────────


async def extract_xanimu_qualities(
    url: str,
    debug_callback: Optional[ProgressCallback] = None,
) -> Tuple[List[dict], str, dict, DebugLog]:
    """
    استخراج کیفیت‌ها با Playwright bypass.

    Returns:
        (qualities, title, info, debug_log)
    """
    dbg = DebugLog()
    dbg.step(f"URL: `{url}`")

    if not is_xanimu_url(url):
        dbg.err("URL نامعتبر")
        if debug_callback:
            await debug_callback(dbg.build_short())
        return [], "Invalid URL", {}, dbg

    dbg.ok("URL معتبر")

    # مرحله 1: حل Cloudflare و گرفتن HTML
    html, cookies, user_agent = await _solve_cloudflare(url, dbg, debug_callback)

    if not html or "Just a moment" in html or len(html) < 2000:
        dbg.err("Cloudflare bypass ناموفق")
        if debug_callback:
            await debug_callback(dbg.build_short())
        return [], "Cloudflare blocked", {}, dbg

    dbg.ok(f"HTML دریافت شد: {len(html)} chars")

    # مرحله 2: آنالیز و استخراج
    _debug_html_structure(html, dbg)

    if debug_callback:
        await debug_callback("🔄 **استخراج لینک‌ها...**")

    qualities, title = _extract_from_html(html, dbg)
    info = _extract_video_info(html, dbg)

    # حذف تکراری
    unique = {}
    for q in qualities:
        key = q.get("quality_key", q.get("url"))
        if key not in unique:
            unique[key] = q
    qualities = sorted(unique.values(), key=lambda q: q.get("height", 0), reverse=True)

    # ذخیره cookies و user_agent در هر quality برای دانلود
    for q in qualities:
        q["_cookies"] = cookies
        q["_user_agent"] = user_agent

    if qualities:
        dbg.ok(f"✅ {len(qualities)} کیفیت: {title[:60]}")
    else:
        dbg.err("❌ هیچ کیفیتی پیدا نشد")

    if debug_callback:
        await debug_callback(dbg.build_short())

    return qualities, title, info, dbg


# ─── Download (با Cloudflare cookies) ──────────────────────


async def download_xanimu_video(
    url: str,
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    cookies: Optional[list] = None,
    user_agent: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود ویدیو.
    cookies و user_agent از extract_xanimu_qualities میان.
    """
    if not is_xanimu_url(url):
        return False, "URL not allowed", 0
    if not video_url:
        return False, "Empty video URL", 0

    dbg = DebugLog()
    ua = user_agent or _USER_AGENT
    cks = cookies or []

    # تلاش 1: دانلود مستقیم با cookies صفحه
    dbg.step("دانلود مستقیم با cookies...")
    success, error, size = await _download_with_cookies(
        video_url, url, filepath, cks, ua, progress_cb, dbg,
    )
    if success:
        return True, "", size

    # تلاش 2: حل Cloudflare CDN و دانلود
    dbg.step("حل Cloudflare CDN...")
    if progress_cb:
        await progress_cb("🔄 **CDN هم Cloudflare داره، در حال حل...**")

    cdn_cookies, cdn_ua = await _solve_cdn_cloudflare(
        video_url, cks, ua, dbg, progress_cb,
    )

    success, error, size = await _download_with_cookies(
        video_url, url, filepath, cdn_cookies, cdn_ua, progress_cb, dbg,
    )
    if success:
        return True, "", size

    # تلاش 3: دانلود با Playwright مستقیم
    dbg.step("دانلود با Playwright...")
    return await _download_with_playwright(
        video_url, filepath, progress_cb, dbg,
    )


async def _download_with_cookies(
    video_url: str,
    referer: str,
    filepath: str,
    cookies: list,
    user_agent: str,
    progress_cb: Optional[ProgressCallback],
    dbg: DebugLog,
) -> Tuple[bool, str, int]:
    """دانلود با aiohttp و Cloudflare cookies."""
    headers = {
        "User-Agent": user_agent,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            jar = aiohttp.CookieJar(unsafe=True)
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)

            async with aiohttp.ClientSession(
                cookie_jar=jar, timeout=timeout
            ) as session:
                # تنظیم cookies
                for cookie in cookies:
                    domain = cookie.get("domain", "").lstrip(".")
                    if domain:
                        session.cookie_jar.update_cookies(
                            {cookie["name"]: cookie["value"]},
                            response_url=aiohttp.client.URL(f"https://{domain}"),
                        )

                async with session.get(
                    video_url, headers=headers, allow_redirects=True
                ) as resp:
                    dbg.info(f"Download attempt {attempt}: HTTP {resp.status}")
                    dbg.info(f"Content-Type: {resp.headers.get('Content-Type', '?')}")

                    content_type = resp.headers.get("Content-Type", "")

                    # اگه HTML برگشت = Cloudflare challenge
                    if "text/html" in content_type:
                        body_preview = await resp.content.read(500)
                        dbg.warn(f"HTML response (CF challenge): `{body_preview.decode(errors='replace')[:200]}`")
                        return False, "Cloudflare challenge on CDN", 0

                    if resp.status != 200:
                        if 400 <= resp.status < 500:
                            return False, f"HTTP {resp.status}", 0
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(RETRY_DELAY * attempt)
                            continue
                        return False, f"HTTP {resp.status}", 0

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

            dbg.ok(f"دانلود موفق: {_format_size(size)}")
            return True, "", size

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            dbg.warn(f"Download attempt {attempt}: {e}")
            _cleanup_file(filepath)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY * attempt)

    return False, f"Failed after {MAX_RETRIES} attempts", 0


async def _download_with_playwright(
    video_url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback],
    dbg: DebugLog,
) -> Tuple[bool, str, int]:
    """
    آخرین چاره: دانلود مستقیم با Playwright.
    مرورگر خودش challenge رو حل می‌کنه و فایل رو دانلود می‌کنه.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "playwright not installed", 0

    dbg.step("دانلود با Playwright browser...")
    if progress_cb:
        await progress_cb("📥 **دانلود با مرورگر (آخرین روش)...**\n⏳ ممکنه کند باشه")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )

            context = await browser.new_context(
                user_agent=_USER_AGENT,
                accept_downloads=True,
            )

            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = {runtime: {}};
            """)

            page = await context.new_page()

            # روش 1: intercept network response
            video_data_chunks = []
            total_size = 0
            download_started = asyncio.Event()

            async def handle_response(response):
                nonlocal total_size
                url = response.url
                content_type = response.headers.get("content-type", "")

                if "video" in content_type or url.endswith(".mp4"):
                    dbg.ok(f"Video response intercepted: {url[:80]}")
                    download_started.set()

                    try:
                        body = await response.body()
                        total_size = len(body)
                        dbg.ok(f"Video size: {_format_size(total_size)}")

                        async with aiofiles.open(filepath, "wb") as f:
                            await f.write(body)
                    except Exception as e:
                        dbg.warn(f"Response body error: {e}")

            page.on("response", handle_response)

            # رفتن به URL
            try:
                await page.goto(video_url, wait_until="networkidle", timeout=120000)
            except Exception as e:
                dbg.warn(f"Playwright goto: {e}")

            # صبر برای دانلود
            try:
                await asyncio.wait_for(download_started.wait(), timeout=60)
            except asyncio.TimeoutError:
                dbg.warn("Video response intercepted نشد")

            await browser.close()

            if os.path.exists(filepath):
                size = os.path.getsize(filepath)
                if size >= MIN_FILE_SIZE:
                    dbg.ok(f"Playwright download: {_format_size(size)}")
                    return True, "", size
                _cleanup_file(filepath)
                return False, f"Too small ({size} bytes)", 0

            return False, "File not created", 0

    except Exception as e:
        dbg.err(f"Playwright download error: {e}")
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


# ─── Wrapper ────────────────────────────────────────────────


async def download_xanimu_direct(
    url: str,
    filepath: str,
    progress_cb: Optional[ProgressCallback] = None,
    video_url: str = "",
    cookies: Optional[list] = None,
    user_agent: Optional[str] = None,
) -> Tuple[bool, str, int]:
    return await download_xanimu_video(
        url, video_url, filepath, progress_cb, cookies, user_agent,
    )
