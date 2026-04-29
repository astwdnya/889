#!/usr/bin/env python3
# Telegram Ultimate Bot - FIXED VERSION v2
# + Upload Progress Bar
# + YouTube via Dirpy Fixed
# + LuxureTV 403 Fixed (Origin header)
# + StopPropagation Bug Fixed

import asyncio
import os
import re
import sys
import logging
import time
import json
from datetime import datetime
from urllib.parse import quote
from typing import Optional, Tuple, Dict

from flask import Flask
from threading import Thread

import aiohttp
import aiofiles
from aiohttp import ClientTimeout

import yt_dlp
from playwright.async_api import async_playwright

from telethon import TelegramClient, events, Button
from telethon.tl.types import Message
from telethon.errors import MessageNotModifiedError

# ====================== CONFIGURATION ======================
BOT_TOKEN = "7675664254:AAHL7QhPonc47z0QKRFnB5p_L15SRiLBddc"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

AUTHORIZED_USERS = {818185073, 6936101187, 7972834913}

MAX_FILE_SIZE_MB = 2000
DOWNLOAD_TIMEOUT = 300
OUTPUT_FOLDER = "output_files"

os.makedirs(OUTPUT_FOLDER, exist_ok=True)
HEALTH_PORT = int(os.environ.get('PORT', 10000))

video_cache: Dict[str, Dict] = {}
user_state: Dict[int, Dict] = {}

# ====================== LOGGING ======================
logging.basicConfig(
    format='%(asctime)s | %(levelname)s | %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("UltimateBot")

# ====================== FLASK KEEP-ALIVE ======================
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "✅ Bot is running!", 200

def start_keep_alive():
    Thread(target=lambda: flask_app.run(host='0.0.0.0', port=HEALTH_PORT, debug=False), daemon=True).start()
    logger.info(f"Keep-alive started on port {HEALTH_PORT}")

# ====================== UTILITIES ======================
def human_readable_size(num_bytes: int) -> str:
    if num_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} TB"

def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds <= 0:
        return "Unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}:{seconds%60:02d}"
    else:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}:{m:02d}"

def safe_filename(title: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', title.strip()[:80]) or f"file_{int(time.time())}"

def parse_size_input(text: str) -> Optional[int]:
    text = text.strip().lower().replace(" ", "")
    match = re.match(r'(\d+\.?\d*)([kmg]?)b?', text)
    if not match:
        return None
    num = float(match.group(1))
    unit = match.group(2)
    if unit == 'k': return int(num * 1024)
    elif unit == 'm': return int(num * 1024 * 1024)
    elif unit == 'g': return int(num * 1024 * 1024 * 1024)
    return int(num)

# ====================== PROGRESS HANDLER ======================
class ProgressHandler:
    def __init__(self, status_message: Message, total_size: int = 0, operation: str = "Downloading"):
        self.status_message = status_message
        self.total_size = total_size
        self.operation = operation
        self.last_update_time = 0
        self.last_bytes = 0
        self._lock = asyncio.Lock()

    async def update(self, current_bytes: int):
        async with self._lock:
            now = time.time()
            if now - self.last_update_time < 1.3 and current_bytes != self.total_size:
                return
            if self.total_size > 0:
                percent = (current_bytes / self.total_size) * 100
                speed = (current_bytes - self.last_bytes) / (now - self.last_update_time) if self.last_update_time > 0 else 0
                eta = (self.total_size - current_bytes) / speed if speed > 0 else 0
                bar = '█' * int(18 * current_bytes // self.total_size) + '░' * (18 - int(18 * current_bytes // self.total_size))
                text = f"**{self.operation}**\n`[{bar}]` **{percent:.1f}%**\n📦 {human_readable_size(current_bytes)} / {human_readable_size(self.total_size)}\n🚀 {human_readable_size(int(speed))}/s"
            else:
                text = f"**{self.operation}...**\n📥 {human_readable_size(current_bytes)}"
            try:
                await self.status_message.edit(text, parse_mode='markdown')
            except Exception:
                pass
            self.last_update_time = now
            self.last_bytes = current_bytes

    async def finish(self, success: bool, msg: str = ""):
        try:
            if success:
                await self.status_message.delete()
            else:
                await self.status_message.edit(f"❌ {msg}", parse_mode='markdown')
        except Exception:
            pass

# ====================== DOWNLOAD FUNCTIONS ======================
async def download_direct_with_progress(url: str, status_msg: Message, referer: Optional[str] = None) -> Tuple[Optional[str], Optional[str], int]:
    MAX_RETRIES = 3
    CHUNK_SIZE = 512 * 1024  # 512KB chunks

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Encoding': 'identity',
        'Connection': 'keep-alive',
    }
    if referer:
        headers['Referer'] = referer
        # FIX: اضافه کردن Origin header برای رفع 403 روی LuxureTV و سایت‌های مشابه
        try:
            origin = '/'.join(referer.split('/')[:3])
            headers['Origin'] = origin
        except Exception:
            pass

    timeout = ClientTimeout(
        total=None,
        connect=30,
        sock_read=120,
    )

    filepath = os.path.join(OUTPUT_FOLDER, f"video_{int(time.time())}.mp4")
    downloaded = 0
    total = 0

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            attempt_headers = headers.copy()
            if downloaded > 0:
                attempt_headers['Range'] = f'bytes={downloaded}-'
                await safe_edit(status_msg, f"🔄 Retry {attempt}/{MAX_RETRIES} — resuming from {human_readable_size(downloaded)}...")

            connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=300, ssl=False)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.get(url, headers=attempt_headers, allow_redirects=True) as response:
                    if response.status not in (200, 206):
                        return None, f"HTTP {response.status}", 0

                    if total == 0:
                        content_length = int(response.headers.get('content-length', 0))
                        if response.status == 206:
                            content_range = response.headers.get('content-range', '')
                            m = re.search(r'/(\d+)', content_range)
                            total = int(m.group(1)) if m else content_length + downloaded
                        else:
                            total = content_length

                        if total > MAX_FILE_SIZE_MB * 1024 * 1024:
                            return None, f"File too large ({human_readable_size(total)})", 0

                        cd = response.headers.get('Content-Disposition', '')
                        if 'filename=' in cd:
                            fm = re.search(r'filename="?([^";]+)', cd)
                            if fm:
                                ext = os.path.splitext(fm.group(1).strip())[1] or '.mp4'
                                filepath = os.path.join(OUTPUT_FOLDER, f"video_{int(time.time())}{ext}")

                    progress = ProgressHandler(status_msg, total, "📥 Downloading")
                    progress.last_bytes = downloaded

                    write_mode = 'ab' if downloaded > 0 else 'wb'
                    async with aiofiles.open(filepath, write_mode) as f:
                        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                            await f.write(chunk)
                            downloaded += len(chunk)
                            await progress.update(downloaded)

            await progress.finish(True, "")
            return filepath, None, downloaded

        except (aiohttp.ClientError, asyncio.TimeoutError, aiohttp.ServerDisconnectedError) as e:
            logger.warning(f"Download attempt {attempt} failed at {human_readable_size(downloaded)}: {e}")
            if attempt == MAX_RETRIES:
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except Exception:
                    pass
                return None, f"Download failed after {MAX_RETRIES} retries: {str(e)[:80]}", 0
            await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Unexpected download error: {e}")
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass
            return None, str(e)[:100], 0

    return None, "Download failed", 0


def is_video_url(url: str) -> bool:
    """Check if a request URL looks like a direct video stream."""
    u = url.lower()

    SKIP_KEYWORDS = ['thumb', 'preview', 'poster', 'banner', 'logo', 'icon', 'sprite', 'storyboard', 'ad', 'tracking', 'analytics', 'pixel']

    SITE_PATTERNS = [
        ('xnxx-cdn.com',         lambda u: 'mp4' in u),
        ('xnxx.com',             lambda u: 'mp4' in u and '/videos/' in u),
        ('media4.luxuretv.com',  lambda u: '.mp4' in u),
        ('media.luxuretv.com',   lambda u: '.mp4' in u),
        ('luxuretv.com',         lambda u: '.mp4' in u and any(m in u for m in ['media', 'cdn', 'video', 'stream'])),
        ('rule34.xxx',           lambda u: '.mp4' in u or ('video' in u and 'api' not in u)),
        ('rule34video.com',      lambda u: '.mp4' in u),
        ('rdtcdn.com',           lambda u: '.mp4' in u),
        ('ev-ph.rdtcdn.com',     lambda u: '.mp4' in u),
        ('redtube.com',          lambda u: '.mp4' in u),
        ('phncdn.com',           lambda u: '.mp4' in u),
        ('pornhub.com',          lambda u: '.mp4' in u and 'cdn' in u),
        ('ahrimp4',              lambda u: True),
        ('media4',               lambda u: '.mp4' in u),
        # FIX: اضافه کردن یوتیوب
        ('googlevideo.com',      lambda u: 'videoplayback' in u),
    ]

    if any(k in u for k in SKIP_KEYWORDS):
        return False

    for domain, check in SITE_PATTERNS:
        if domain in u and check(u):
            return True

    GENERIC_CDN_SIGNALS = ['-cdn', 'media', 'video', 'stream', 'content', 'storage', 'ev-', 'cdn-', '.cdn']
    if '.mp4' in u and any(sig in u for sig in GENERIC_CDN_SIGNALS):
        return True

    for ext in ['.webm', '.m3u8', '.ts', '.mkv']:
        if ext in u and any(sig in u for sig in GENERIC_CDN_SIGNALS):
            return True

    return False


# ====================== LUXURETV DIRECT EXTRACTOR ======================
async def extract_direct_from_site(video_url: str, status_msg: Message) -> Tuple[list, Optional[str]]:
    """مستقیماً از سورس صفحه سایت همه لینک‌های mp4 رو جمع میکنه."""
    async with async_playwright() as p:
        browser = None
        try:
            await safe_edit(status_msg, "🔍 Extracting video sources directly...")
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
            )
            page = await context.new_page()

            captured_urls: list = []
            seen: set = set()

            async def on_response(response):
                rurl = response.url.lower()
                content_type = response.headers.get('content-type', '')
                content_length = int(response.headers.get('content-length', 0))
                skip = ['thumb', 'preview', 'poster', 'banner', 'sprite', 'storyboard']
                is_video_response = ('video' in content_type or 'octet-stream' in content_type)
                is_cdn_mp4 = '.mp4' in rurl and any(s in rurl for s in [
                    'media', 'cdn', 'rdtcdn', 'ev-', 'stream',
                    'p300cdn', 'p300', 'xvideos-cdn', 'xhamster',
                    'phncdn', 'xnxx-cdn',
                ])
                if (is_video_response or is_cdn_mp4) and content_length > 500 * 1024 and not any(k in rurl for k in skip):
                    url_norm = response.url.split('?')[0]
                    if url_norm not in seen:
                        seen.add(url_norm)
                        captured_urls.append(response.url)
                        logger.info(f"[DIRECT] Captured: {response.url[:200]}")

            page.on('response', on_response)

            await page.goto(video_url, wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(5000)
            await page.evaluate('() => { const v = document.querySelector("video"); if(v) { v.muted=true; v.play(); } }')
            await page.wait_for_timeout(8000)

            if not captured_urls:
                html = await page.content()
                pat1 = re.compile(r'https?://\S+?\.mp4(?:\?\S+)?')
                for m in pat1.findall(html):
                    ml = m.lower()
                    url_norm = m.split('?')[0]
                    if url_norm not in seen and not any(k in ml for k in ['thumb', 'preview', 'poster']):
                        seen.add(url_norm)
                        captured_urls.append(m)

            if captured_urls:
                return captured_urls, None
            return [], "Could not find video source"
        except Exception as e:
            logger.error(f"Direct extract error: {e}")
            return [], str(e)
        finally:
            if browser:
                await browser.close()

async def extract_video_url_smart(video_url: str, status_msg: Message) -> Tuple[list, Optional[str]]:
    """
    فقط از Dirpy استفاده میکنه - هیچ وقت به سایت اصلی مراجعه نمیکنه.
    فیلتر سایز: فقط فایل‌های > 1MB یا CDN‌های شناخته‌شده.

    FIX: اضافه شدن googlevideo.com برای یوتیوب
    """
    SKIP = ['thumb', 'preview', 'poster', 'banner', 'logo', 'icon', 'sprite',
            'storyboard', 'tracking', 'analytics', 'pixel', 'ad/', '/ads/']
    MIN_SIZE = 2 * 1024 * 1024  # حداقل 2MB

    async with async_playwright() as p:
        browser = None
        captured_urls: list = []
        seen: set = set()

        def add_url(u: str, source: str = ""):
            ul = u.lower()
            if any(k in ul for k in SKIP):
                return
            norm = u.split('?')[0]
            if norm in seen:
                return
            seen.add(norm)
            captured_urls.append(u)
            logger.info(f"[{source}] {u[:180]}")

        try:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1280, 'height': 720}
            )

            async def run_dirpy_page(target_url: str, label: str):
                page = await context.new_page()
                try:
                    async def on_response(response):
                        try:
                            ct = response.headers.get('content-type', '')
                            cl = int(response.headers.get('content-length', 0))
                            ru = response.url
                            rul = ru.lower()
                            if any(k in rul for k in SKIP):
                                return

                            # شرط ۱: content-type ویدیو + حجم بالای MIN_SIZE
                            if ('video/' in ct) and cl > MIN_SIZE:
                                add_url(ru, f"RESP-CT {label}")
                                return

                            # FIX: لیست CDN‌های شناخته‌شده - اضافه شدن googlevideo.com برای یوتیوب
                            is_cdn = any(d in rul for d in [
                                'rdtcdn.com', 'phncdn.com', 'xnxx-cdn.com',
                                'media4.luxuretv', 'media.luxuretv',
                                'rule34.xxx', 'rule34video',
                                'kv-ph.', 'ev-ph.', 'di-ph.',
                                'googlevideo.com',   # ← یوتیوب
                                'videoplayback',     # ← یوتیوب fallback
                            ])

                            # FIX: شرط mp4 یا videoplayback (برای یوتیوب که extension نداره)
                            is_video_path = '.mp4' in rul or 'videoplayback' in rul

                            if is_cdn and is_video_path:
                                # اگه content-length داره باید بالای MIN_SIZE باشه
                                # یوتیوب معمولاً content-length نمیده پس cl==0 رو قبول میکنیم
                                if cl == 0 or cl > MIN_SIZE:
                                    add_url(ru, f"RESP-CDN {label}")
                        except Exception:
                            pass

                    page.on('response', on_response)

                    await page.goto(target_url, wait_until='domcontentloaded', timeout=60000)
                    await page.wait_for_timeout(7000)

                    if not captured_urls:
                        await page.evaluate('() => { try { document.querySelector("video")?.play(); } catch(e){} }')
                        await page.wait_for_timeout(8000)

                    if not captured_urls:
                        # scan HTML برای CDN لینک‌های شناخته‌شده
                        html = await page.content()
                        cdn_domains = ['rdtcdn.com', 'phncdn.com', 'xnxx-cdn.com',
                                       'media4.luxuretv', 'media.luxuretv',
                                       'kv-ph.', 'ev-ph.', 'di-ph.',
                                       'rule34video', 'p300cdn',
                                       'googlevideo.com']  # ← یوتیوب
                        for m in re.findall(r"https?://[^\x22\x27<>\s]+[^\x22\x27<>\s]*", html):
                            ml = m.lower()
                            if any(d in ml for d in cdn_domains):
                                if '.mp4' in ml or 'videoplayback' in ml:
                                    add_url(m, f"HTML-CDN {label}")
                finally:
                    await page.close()

            await safe_edit(status_msg, "🔗 Opening Dirpy Studio...")
            dirpy_url = f"https://dirpy.com/studio?url={quote(video_url)}"
            await run_dirpy_page(dirpy_url, "DIRPY")

            if captured_urls:
                return captured_urls, None
            return [], "Could not capture video link via Dirpy"

        except Exception as e:
            logger.error(f"Interception error: {e}")
            return [], str(e)
        finally:
            if browser:
                await browser.close()


async def html_to_pdf(url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str], int]:
    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True, args=[
                '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-blink-features=AutomationControlled'
            ])
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await safe_edit(status_msg, "🌐 Loading page...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            except Exception:
                pass

            try:
                age_selectors = [
                    'button:has-text("I AM 18")',
                    'button:has-text("I am 18")',
                    'button:has-text("ENTER")',
                    'button:has-text("Enter")',
                    'button:has-text("over 18")',
                    'button:has-text("Yes")',
                    'button:has-text("Confirm")',
                    'a:has-text("I AM 18")',
                    'a:has-text("ENTER")',
                    '.age-gate button',
                    '#age-gate button',
                    '[class*="age"] button',
                    'button.y',
                ]
                for sel in age_selectors:
                    try:
                        el = page.locator(sel).first
                        if await el.is_visible(timeout=1000):
                            await el.click()
                            logger.info(f"Age gate bypassed with: {sel}")
                            await asyncio.sleep(2)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            await safe_edit(status_msg, "📜 Scrolling to load all images...")
            await page.evaluate("""
                async () => {
                    const delay = ms => new Promise(r => setTimeout(r, ms));
                    const totalHeight = document.body.scrollHeight;
                    const step = Math.floor(window.innerHeight * 0.8);
                    let current = 0;
                    while (current < totalHeight) {
                        window.scrollTo(0, current);
                        await delay(300);
                        current += step;
                    }
                    window.scrollTo(0, totalHeight);
                    await delay(500);
                }
            """)
            await asyncio.sleep(4)

            await safe_edit(status_msg, "📄 Rendering PDF...")
            filepath = os.path.join(OUTPUT_FOLDER, f"pdf_{int(time.time())}.pdf")
            await page.pdf(
                path=filepath,
                format="A4",
                print_background=True,
                margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"}
            )
            size = os.path.getsize(filepath)
            return filepath, None, size
        except Exception as e:
            logger.error(f"PDF Error: {e}")
            return None, f"PDF Error: {str(e)[:80]}", 0
        finally:
            if browser:
                await browser.close()


# ====================== CAPTURE MHTML ======================
async def capture_mhtml(url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str], int]:
    async with async_playwright() as p:
        browser = None
        try:
            await status_msg.edit("🌐 Capturing full webpage as MHTML...")
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(3)

            client = await context.new_cdp_session(page)
            result = await client.send("Page.captureSnapshot", {"format": "mhtml"})
            mhtml_data = result.get("data", "")

            if not mhtml_data:
                return None, "Failed to capture MHTML", 0

            filepath = os.path.join(OUTPUT_FOLDER, f"page_{int(time.time())}.mhtml")
            async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
                await f.write(mhtml_data)

            size = os.path.getsize(filepath)
            return filepath, None, size
        except Exception as e:
            logger.error(f"MHTML Error: {e}")
            return None, f"MHTML Error: {str(e)[:80]}", 0
        finally:
            if browser:
                await browser.close()


# ====================== VIDEO COMPRESSION ======================
async def compress_video(input_path: str, target_size_bytes: int, status_msg: Message) -> Tuple[Optional[str], str]:
    output_path = input_path.replace(".mp4", f"_compressed_{int(target_size_bytes/1024/1024)}mb.mp4")

    await safe_edit(status_msg, f"⚙️ Compressing video to ≈ {human_readable_size(target_size_bytes)}...")

    try:
        cmd_duration = f'ffprobe -v quiet -print_format json -show_format "{input_path}"'
        proc = await asyncio.create_subprocess_shell(
            cmd_duration,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        info = json.loads(stdout.decode())
        duration = float(info['format']['duration'])

        target_bitrate = int((target_size_bytes * 8) / duration * 0.92)

        cmd = [
            'ffmpeg', '-i', input_path,
            '-vcodec', 'libx264', '-crf', '28',
            '-b:v', str(target_bitrate),
            '-preset', 'medium',
            '-acodec', 'aac', '-b:a', '128k',
            '-y', output_path
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            logger.error(f"FFmpeg Error: {stderr.decode()[:300]}")
            return None, "FFmpeg compression failed"

        final_size = os.path.getsize(output_path)
        return output_path, f"Compressed to {human_readable_size(final_size)}"

    except Exception as e:
        logger.error(f"Compression error: {e}")
        return None, str(e)


# ====================== SAFE EDIT ======================
async def safe_edit(msg: Message, text: str):
    try:
        await msg.edit(text, parse_mode='markdown')
    except Exception:
        pass


# ====================== GET FILE SIZE VIA HEAD ======================
async def get_file_size(url: str) -> int:
    """حجم فایل رو با HEAD request میگیره."""
    try:
        timeout = ClientTimeout(connect=10, sock_read=10, total=15)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(url, headers=headers, allow_redirects=True, ssl=False) as resp:
                return int(resp.headers.get('content-length', 0))
    except Exception:
        return 0


def _url_label(url: str, size: int, index: int) -> str:
    """لیبل دکمه برای هر لینک."""
    u = url.lower()
    quality = "Unknown"
    for q in ['2160p', '1080p', '720p', '480p', '360p', '240p', '4k', 'hd', 'sd', 'hq', 'lq']:
        if q in u:
            quality = q.upper()
            break
    sz_str = human_readable_size(size) if size > 0 else "? MB"
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc.replace('www.', '')[:20]
    except Exception:
        domain = f"Link {index+1}"
    return f"#{index+1} {quality} • {sz_str} • {domain}"


# ====================== UPLOAD WITH PROGRESS BAR ======================
async def send_file_with_progress(
    client,
    chat_id: int,
    filepath: str,
    caption: str,
    status_msg: Message,
    buttons=None,
    supports_streaming: bool = True
):
    """
    ارسال فایل به تلگرام با پروگرس بار آپلود.
    کاربر میتونه ببینه چند درصد آپلود شده، سرعت چقدره، و چقدر زمان مونده.
    """
    file_size = os.path.getsize(filepath)
    start_time = time.time()
    last_update = [0.0]  # list به جای nonlocal برای سازگاری بیشتر

    async def upload_progress_callback(current: int, total: int):
        now = time.time()
        # هر ۲ ثانیه یه بار آپدیت میکنیم - نه خیلی زیاد که flood بشه
        if now - last_update[0] < 2.0 and current != total:
            return
        last_update[0] = now

        elapsed = now - start_time
        speed = current / elapsed if elapsed > 0 else 0
        eta = (total - current) / speed if speed > 0 else 0
        percent = (current / total) * 100 if total > 0 else 0
        filled = int(18 * current // total) if total > 0 else 0
        bar = '█' * filled + '░' * (18 - filled)

        # فرمت زمان باقیمانده
        if eta < 60:
            eta_str = f"{int(eta)}s"
        elif eta < 3600:
            eta_str = f"{int(eta // 60)}:{int(eta % 60):02d}"
        else:
            eta_str = f"{int(eta // 3600)}h {int((eta % 3600) // 60)}m"

        text = (
            f"**📤 Uploading...**\n"
            f"`[{bar}]` **{percent:.1f}%**\n"
            f"📦 {human_readable_size(current)} / {human_readable_size(total)}\n"
            f"🚀 {human_readable_size(int(speed))}/s  •  ⏱ {eta_str}"
        )
        try:
            await status_msg.edit(text, parse_mode='markdown')
        except Exception:
            pass

    await client.send_file(
        chat_id,
        filepath,
        caption=caption,
        supports_streaming=supports_streaming,
        buttons=buttons,
        parse_mode='markdown',
        progress_callback=upload_progress_callback
    )


# ====================== DOWNLOAD AND SEND ======================
async def do_download_and_send(event, status_msg, direct_url: str, source_url: str):
    """دانلود و ارسال ویدیو به کاربر - با پروگرس بار هم دانلود هم آپلود."""
    await safe_edit(status_msg, "📥 Downloading video...")
    filepath, dl_error, final_size = await download_direct_with_progress(direct_url, status_msg, referer=source_url)

    if dl_error or not filepath:
        await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
        return

    video_id = f"vid_{event.chat_id}_{int(time.time())}"
    video_cache[video_id] = {
        "filepath": filepath,
        "chat_id": event.chat_id,
        "original_size": final_size,
        "original_url": source_url
    }

    buttons = [
        [Button.inline("🗜 Compress Video", f"compress_{video_id}")],
        [Button.inline("✅ Check (Delete)", f"check_{video_id}")]
    ]

    # نشون دادن پیام شروع آپلود
    await safe_edit(status_msg, "📤 Uploading...")

    try:
        # آپلود با پروگرس بار
        await send_file_with_progress(
            client=event.client,
            chat_id=event.chat_id,
            filepath=filepath,
            caption=(
                f"🎬 **Video Downloaded**\n"
                f"📦 Size: {human_readable_size(final_size)}\n"
                f"🔗 [Source]({source_url})\n"
                f"⬇️ [DW Link]({direct_url})"
            ),
            status_msg=status_msg,
            buttons=buttons,
            supports_streaming=True
        )
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
        return

    try:
        await status_msg.delete()
    except Exception:
        pass


processing_messages = set()

async def process_dirpy_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)

    status_msg = await event.reply("🔄 Starting Dirpy extraction...", parse_mode='markdown')

    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        found_urls, intercept_err = await extract_video_url_smart(url, status_msg)

        if not found_urls:
            await safe_edit(status_msg, f"❌ Could not capture video via Dirpy:\n{intercept_err}")
            return

        if len(found_urls) == 1:
            await do_download_and_send(event, status_msg, found_urls[0], url)
            return

        await safe_edit(status_msg, f"🔍 Found {len(found_urls)} links, checking sizes...")
        sized_urls = []
        for u in found_urls:
            sz = await get_file_size(u)
            sized_urls.append((u, sz))

        pick_id = f"pick_{event.chat_id}_{int(time.time())}"
        video_cache[pick_id] = {
            "urls": sized_urls,
            "source_url": url,
            "chat_id": event.chat_id
        }

        buttons = []
        for i, (u, sz) in enumerate(sized_urls):
            label = _url_label(u, sz, i)
            buttons.append([Button.inline(label, f"pickurl_{pick_id}_{i}")])

        await safe_edit(status_msg, "📋 **Select video to download:**")
        await event.client.send_message(
            event.chat_id,
            f"🎬 Found **{len(sized_urls)}** video links for:\n{url}\n\nChoose one to download:",
            buttons=buttons,
            parse_mode='markdown'
        )
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Dirpy process error: {e}", exc_info=True)
        await safe_edit(status_msg, f"❌ Unexpected error: {str(e)[:120]}")
    finally:
        processing_messages.discard(msg_id)


# ====================== CALLBACK HANDLERS ======================
@events.register(events.CallbackQuery(pattern=r"compress_(.+)"))
async def compress_callback(event):
    video_id = event.pattern_match.group(1).decode() if isinstance(event.pattern_match.group(1), bytes) else event.pattern_match.group(1)
    if video_id not in video_cache:
        return await event.answer("Video not found or expired.", alert=True)

    await event.answer("Send desired size (e.g: 15mb or 800kb)", alert=False)
    user_state[event.chat_id] = {"action": "wait_for_compression_size", "video_id": video_id}


@events.register(events.CallbackQuery(pattern=r"check_(.+)"))
async def check_callback(event):
    video_id = event.pattern_match.group(1).decode() if isinstance(event.pattern_match.group(1), bytes) else event.pattern_match.group(1)
    if video_id not in video_cache:
        return await event.answer("Video already deleted.", alert=True)

    data = video_cache[video_id]
    try:
        if os.path.exists(data["filepath"]):
            os.remove(data["filepath"])
        await event.answer("✅ Video deleted from server.", alert=False)
        await event.edit(buttons=None)
    except Exception as e:
        logger.error(f"Delete error: {e}")
        await event.answer("Error deleting file.", alert=True)

    if video_id in video_cache:
        del video_cache[video_id]


@events.register(events.CallbackQuery(pattern=r"pickurl_(.+)_(\d+)$"))
async def pickurl_callback(event):
    raw = event.pattern_match.group(1).decode() if isinstance(event.pattern_match.group(1), bytes) else event.pattern_match.group(1)
    idx_raw = event.pattern_match.group(2).decode() if isinstance(event.pattern_match.group(2), bytes) else event.pattern_match.group(2)
    pick_id = raw
    idx = int(idx_raw)

    if pick_id not in video_cache:
        return await event.answer("Session expired. Please resend /dirpy command.", alert=True)

    data = video_cache[pick_id]
    urls = data["urls"]

    if idx >= len(urls):
        return await event.answer("Invalid selection.", alert=True)

    chosen_url, _ = urls[idx]
    source_url = data["source_url"]

    await event.answer(f"Starting download #{idx+1}...", alert=False)

    try:
        await event.delete()
    except Exception:
        pass

    status_msg = await event.client.send_message(event.chat_id, "📥 Starting download...")
    del video_cache[pick_id]

    await do_download_and_send(event, status_msg, chosen_url, source_url)


# ====================== SIZE INPUT HANDLER ======================
@events.register(events.NewMessage(incoming=True))
async def size_input_handler(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    if event.chat_id not in user_state:
        return

    state = user_state.get(event.chat_id)
    if not state or state.get("action") != "wait_for_compression_size":
        return

    # FIX: StopPropagation باید آخر بیاد تا منطق اصلی اجرا بشه
    # قبلاً اول بود و هیچ‌وقت کد زیرش اجرا نمیشد!

    video_id = state["video_id"]
    if video_id not in video_cache:
        user_state.pop(event.chat_id, None)
        raise events.StopPropagation

    target_bytes = parse_size_input(event.raw_text)
    if not target_bytes:
        await event.reply("❌ Invalid size format!\nExamples: `15mb`, `800kb`, `1.5gb`", parse_mode='markdown')
        raise events.StopPropagation

    data = video_cache[video_id]
    if target_bytes >= data["original_size"]:
        await event.reply("❌ Target size must be smaller than original size.", parse_mode='markdown')
        raise events.StopPropagation

    status_msg = await event.reply(f"⚙️ Compressing to {human_readable_size(target_bytes)}...")

    compressed_path, result = await compress_video(data["filepath"], target_bytes, status_msg)

    if compressed_path and os.path.exists(compressed_path):
        # آپلود ویدیو فشرده‌شده هم با پروگرس بار
        await safe_edit(status_msg, "📤 Uploading compressed video...")
        try:
            await send_file_with_progress(
                client=event.client,
                chat_id=event.chat_id,
                filepath=compressed_path,
                caption=(
                    f"✅ **Compressed Video**\n"
                    f"🎯 Requested: {human_readable_size(target_bytes)}\n"
                    f"📦 Final Size: {human_readable_size(os.path.getsize(compressed_path))}"
                ),
                status_msg=status_msg,
                supports_streaming=True
            )
        except Exception as e:
            logger.error(f"Compressed upload error: {e}")
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")

        try:
            await status_msg.delete()
        except Exception:
            pass

        try:
            os.remove(compressed_path)
            os.remove(data["filepath"])
        except Exception:
            pass
    else:
        await safe_edit(status_msg, f"❌ Compression failed: {result}")

    user_state.pop(event.chat_id, None)
    video_cache.pop(video_id, None)
    raise events.StopPropagation  # ← اینجا باید باشه، نه اول تابع


# ====================== PDF & HTML COMMANDS ======================
async def process_pdf_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)

    status = await event.reply("📄 Converting to PDF...", parse_mode='markdown')
    filepath = None

    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        filepath, error, size = await html_to_pdf(url, status)
        if error:
            await safe_edit(status, f"❌ {error}")
            return
        await event.client.send_file(event.chat_id, filepath, caption=f"📑 PDF • {human_readable_size(size)}", force_document=True)
        await status.delete()
    except Exception as e:
        await safe_edit(status, f"❌ Unexpected error: {str(e)[:120]}")
    finally:
        processing_messages.discard(msg_id)
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


async def process_html_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)

    status = await event.reply("🌐 Capturing full webpage...", parse_mode='markdown')
    filepath = None

    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        filepath, error, size = await capture_mhtml(url, status)
        if error:
            await safe_edit(status, f"❌ {error}")
            return
        await event.client.send_file(event.chat_id, filepath, caption="📦 Complete Webpage Snapshot (MHTML)")
        await status.delete()
    except Exception as e:
        await safe_edit(status, f"❌ Unexpected error: {str(e)[:120]}")
    finally:
        processing_messages.discard(msg_id)
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


# ====================== TELEGRAM HANDLERS ======================
@events.register(events.NewMessage(pattern='/start', incoming=True))
async def start_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    await event.reply(
        "🚀 **Ultimate Bot - Fixed Version**\n\n"
        "• `/dirpy <url>` → Download via Dirpy (direct)\n"
        "• `/pdf <url>` → Webpage to PDF\n"
        "• `/html <url>` → Save as MHTML\n\n"
        "After video sent, use:\n"
        "🗜 Compress Video\n"
        "✅ Check (Delete from server)",
        parse_mode='markdown'
    )


@events.register(events.NewMessage(pattern='/dirpy', incoming=True))
async def dirpy_command(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/dirpy <url>`", parse_mode='markdown')
    await process_dirpy_request(event, parts[1].strip())


@events.register(events.NewMessage(pattern='/pdf', incoming=True))
async def pdf_command(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/pdf <url>`", parse_mode='markdown')
    await process_pdf_request(event, parts[1].strip())


@events.register(events.NewMessage(pattern='/html', incoming=True))
async def html_command(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/html <url>`", parse_mode='markdown')
    await process_html_request(event, parts[1].strip())


@events.register(events.NewMessage(incoming=True))
async def generic_url_handler(event):
    if event.sender_id not in AUTHORIZED_USERS or event.raw_text.startswith('/'):
        return
    if event.chat_id in user_state and user_state[event.chat_id].get("action") == "wait_for_compression_size":
        return
    urls = re.findall(r'https?://[^\s<>"\']+', event.raw_text)
    if not urls:
        return
    status_msg = await event.reply("⏬ Downloading direct link...")
    filepath, error, size = await download_direct_with_progress(urls[0], status_msg)
    if error or not filepath:
        await safe_edit(status_msg, f"❌ {error or 'Failed'}")
        return

    await safe_edit(status_msg, "📤 Uploading...")
    try:
        await send_file_with_progress(
            client=event.client,
            chat_id=event.chat_id,
            filepath=filepath,
            caption=f"📦 {human_readable_size(size)}",
            status_msg=status_msg,
            supports_streaming=True
        )
    except Exception as e:
        await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
        return

    try:
        await status_msg.delete()
    except Exception:
        pass
    try:
        os.remove(filepath)
    except Exception:
        pass


# ====================== MAIN ======================
async def main():
    print("\n" + "="*80)
    print("🚀 ULTIMATE BOT - FIXED VERSION v2")
    print("   + Upload Progress Bar")
    print("   + YouTube/googlevideo.com Fixed")
    print("   + LuxureTV 403 Fixed (Origin header)")
    print("   + StopPropagation Bug Fixed")
    print("="*80)

    start_keep_alive()

    client = TelegramClient('ultimate_bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    client.add_event_handler(start_cmd)
    client.add_event_handler(dirpy_command)
    client.add_event_handler(pdf_command)
    client.add_event_handler(html_command)
    client.add_event_handler(compress_callback)
    client.add_event_handler(check_callback)
    client.add_event_handler(pickurl_callback)
    client.add_event_handler(size_input_handler)
    client.add_event_handler(generic_url_handler)

    me = await client.get_me()
    logger.info(f"✅ Bot started as @{me.username}")
    print(f"✅ Bot is online → @{me.username}")

    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
