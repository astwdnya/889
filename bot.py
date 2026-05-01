#!/usr/bin/env python3
# Telegram Ultimate Bot - v5
# Fixes: 403 auto-dirpy + FFmpeg scale/rotation fix + size_input chat_id fix + pause/resume split

import asyncio
import os
import re
import sys
import logging
import time
import json
from urllib.parse import quote
from typing import Optional, Tuple, Dict

from flask import Flask
from threading import Thread

import aiohttp
import aiofiles
from aiohttp import ClientTimeout

from playwright.async_api import async_playwright

from telethon import TelegramClient, events, Button
from telethon.tl.types import Message, DocumentAttributeVideo

# ====================== CONFIGURATION ======================
BOT_TOKEN = "7675664254:AAGzV0-hpFhq-1jmeAB3QQwpYWKy3phYOUo"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

AUTHORIZED_USERS = {818185073, 6936101187, 7972834913}
ADMIN_ID = 818185073

MAX_FILE_SIZE_MB = 2000
OUTPUT_FOLDER = "output_files"

os.makedirs(OUTPUT_FOLDER, exist_ok=True)
HEALTH_PORT = int(os.environ.get('PORT', 10000))

video_cache: Dict[str, Dict] = {}
user_state: Dict[int, Dict] = {}
admin_pending_add: Dict[int, bool] = {}
active_downloads: Dict[str, Dict] = {}

# ====================== LOGGING ======================
logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s', level=logging.INFO)
logger = logging.getLogger("UltimateBot")

# ====================== FLASK KEEP-ALIVE ======================
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "OK", 200

def start_keep_alive():
    Thread(target=lambda: flask_app.run(host='0.0.0.0', port=HEALTH_PORT, debug=False), daemon=True).start()

# ====================== UTILITIES ======================
def human_readable_size(num_bytes: int) -> str:
    if num_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} TB"

def safe_filename(title: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', title.strip()[:80]) or f"file_{int(time.time())}"

def parse_size_input(text: str) -> Optional[int]:
    # FIX: regex محکم‌تر — فقط عدد+واحد
    text = text.strip().lower().replace(" ", "")
    match = re.match(r'^(\d+\.?\d*)([kmg]?)b?$', text)
    if not match:
        return None
    num = float(match.group(1))
    unit = match.group(2)
    if unit == 'k': return int(num * 1024)
    elif unit == 'm': return int(num * 1024 * 1024)
    elif unit == 'g': return int(num * 1024 * 1024 * 1024)
    return int(num)

async def safe_edit(msg: Message, text: str, buttons=None):
    try:
        if buttons is not None:
            await msg.edit(text, parse_mode='markdown', buttons=buttons)
        else:
            await msg.edit(text, parse_mode='markdown')
    except Exception:
        pass

def build_progress_text(operation: str, current: int, total: int, speed: float, start_time: float) -> str:
    eta = (total - current) / speed if speed > 0 else 0
    percent = (current / total) * 100 if total > 0 else 0
    filled = int(18 * current // total) if total > 0 else 0
    bar = '█' * filled + '░' * (18 - filled)
    if eta < 60:
        eta_str = f"{int(eta)}s"
    elif eta < 3600:
        eta_str = f"{int(eta // 60)}:{int(eta % 60):02d}"
    else:
        eta_str = f"{int(eta // 3600)}h{int((eta % 3600) // 60)}m"
    return (
        f"**{operation}**\n"
        f"`[{bar}]` **{percent:.1f}%**\n"
        f"📦 {human_readable_size(current)} / {human_readable_size(total)}\n"
        f"🚀 {human_readable_size(int(speed))}/s  •  ⏱ {eta_str}"
    )

# ====================== DOWNLOAD WITH PAUSE/CANCEL ======================
async def download_with_controls(
    url: str,
    status_msg: Message,
    dl_id: str,
    referer: Optional[str] = None,
    extra_headers: Optional[dict] = None,
) -> Tuple[Optional[str], Optional[str], int]:
    MAX_RETRIES = 3
    CHUNK_SIZE = 512 * 1024

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Encoding': 'identity',
        'Connection': 'keep-alive',
    }
    if referer:
        headers['Referer'] = referer
        try:
            headers['Origin'] = '/'.join(referer.split('/')[:3])
        except Exception:
            pass
    if extra_headers:
        headers.update(extra_headers)

    timeout = ClientTimeout(total=None, connect=30, sock_read=120)
    filepath = os.path.join(OUTPUT_FOLDER, f"video_{int(time.time())}.mp4")
    downloaded = 0
    total = 0
    last_update = 0.0
    last_bytes_for_speed = 0
    last_time_for_speed = time.time()
    start_time = time.time()

    if dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}

    dl_buttons_pause  = [[Button.inline("⏸ Pause",    f"dlpause_{dl_id}"),  Button.inline("❌ Cancel", f"dlcancel_{dl_id}")]]
    dl_buttons_resume = [[Button.inline("▶️ Resume",  f"dlresume_{dl_id}"), Button.inline("❌ Cancel", f"dlcancel_{dl_id}")]]

    await safe_edit(status_msg, "📥 Connecting...", buttons=dl_buttons_pause)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            attempt_headers = headers.copy()
            if downloaded > 0:
                attempt_headers['Range'] = f'bytes={downloaded}-'
                await safe_edit(status_msg,
                    f"🔄 Retry {attempt}/{MAX_RETRIES} — resuming from {human_readable_size(downloaded)}...",
                    buttons=dl_buttons_pause)

            connector = aiohttp.TCPConnector(limit=1, ttl_dns_cache=300, ssl=False)
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.get(url, headers=attempt_headers, allow_redirects=True) as response:
                    # FIX: 403 رو به عنوان کد خاص برمیگردونه تا caller تصمیم بگیره
                    if response.status == 403:
                        return None, "HTTP_403", 0
                    if response.status not in (200, 206):
                        return None, f"HTTP {response.status}", 0

                    if total == 0:
                        content_length = int(response.headers.get('content-length', 0))
                        if response.status == 206:
                            cr = response.headers.get('content-range', '')
                            m = re.search(r'/(\d+)', cr)
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

                    write_mode = 'ab' if downloaded > 0 else 'wb'
                    async with aiofiles.open(filepath, write_mode) as f:
                        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                            if active_downloads.get(dl_id, {}).get("cancelled"):
                                try:
                                    if os.path.exists(filepath): os.remove(filepath)
                                except Exception: pass
                                try:
                                    await status_msg.edit("🚫 Download cancelled.", buttons=None)
                                except Exception: pass
                                return None, "Cancelled by user", 0

                            if active_downloads.get(dl_id, {}).get("paused"):
                                paused_text = build_progress_text("⏸ Paused", downloaded, total, 0, start_time)
                                await safe_edit(status_msg, paused_text, buttons=dl_buttons_resume)
                                while active_downloads.get(dl_id, {}).get("paused"):
                                    if active_downloads.get(dl_id, {}).get("cancelled"):
                                        try:
                                            if os.path.exists(filepath): os.remove(filepath)
                                        except Exception: pass
                                        try:
                                            await status_msg.edit("🚫 Download cancelled.", buttons=None)
                                        except Exception: pass
                                        return None, "Cancelled by user", 0
                                    await asyncio.sleep(0.5)
                                last_update = 0.0

                            await f.write(chunk)
                            downloaded += len(chunk)

                            now = time.time()
                            if now - last_update >= 1.5 and downloaded != total:
                                dt = now - last_time_for_speed
                                speed = (downloaded - last_bytes_for_speed) / dt if dt > 0 else 0
                                last_bytes_for_speed = downloaded
                                last_time_for_speed = now
                                last_update = now
                                text = build_progress_text("📥 Downloading", downloaded, total, speed, start_time)
                                await safe_edit(status_msg, text, buttons=dl_buttons_pause)

            active_downloads.pop(dl_id, None)
            try:
                await status_msg.edit("✅ Download complete!", parse_mode='markdown', buttons=None)
            except Exception: pass
            return filepath, None, downloaded

        except (aiohttp.ClientError, asyncio.TimeoutError, aiohttp.ServerDisconnectedError) as e:
            logger.warning(f"Download attempt {attempt} failed: {e}")
            if attempt == MAX_RETRIES:
                active_downloads.pop(dl_id, None)
                try:
                    if os.path.exists(filepath): os.remove(filepath)
                except Exception: pass
                return None, f"Failed after {MAX_RETRIES} retries: {str(e)[:80]}", 0
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Unexpected download error: {e}")
            active_downloads.pop(dl_id, None)
            try:
                if os.path.exists(filepath): os.remove(filepath)
            except Exception: pass
            return None, str(e)[:100], 0

    active_downloads.pop(dl_id, None)
    return None, "Download failed", 0


# ====================== PAUSE / RESUME / CANCEL CALLBACKS ======================
# FIX: pause و resume دو callback جدا دارن — قبلاً toggle بود که race condition داشت

@events.register(events.CallbackQuery(pattern=r"dlpause_(.+)"))
async def dl_pause_callback(event):
    dl_id = event.data.decode().replace("dlpause_", "")
    if dl_id not in active_downloads:
        return await event.answer("No active download found.", alert=True)
    active_downloads[dl_id]["paused"] = True
    await event.answer("⏸ Paused!", alert=False)


@events.register(events.CallbackQuery(pattern=r"dlresume_(.+)"))
async def dl_resume_callback(event):
    dl_id = event.data.decode().replace("dlresume_", "")
    if dl_id not in active_downloads:
        return await event.answer("No active download found.", alert=True)
    active_downloads[dl_id]["paused"] = False
    await event.answer("▶️ Resumed!", alert=False)


@events.register(events.CallbackQuery(pattern=r"dlcancel_(.+)"))
async def dl_cancel_callback(event):
    dl_id = event.data.decode().replace("dlcancel_", "")
    if dl_id not in active_downloads:
        return await event.answer("No active download found.", alert=True)
    active_downloads[dl_id]["cancelled"] = True
    active_downloads[dl_id]["paused"] = False
    await event.answer("❌ Cancelling...", alert=False)
    try:
        await event.edit(buttons=None)
    except Exception: pass


# ====================== UPLOAD WITH PROGRESS ======================
async def get_video_thumbnail(filepath: str) -> Optional[str]:
    """یه فریم از وسط ویدیو به عنوان thumbnail می‌گیره"""
    try:
        thumb_path = filepath + "_thumb.jpg"
        # مدت ویدیو رو بگیر تا فریم از وسط باشه
        probe = await asyncio.create_subprocess_exec(
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', filepath,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await probe.communicate()
        duration = 0.0
        try:
            duration = float(json.loads(stdout.decode()).get('format', {}).get('duration', 0))
        except Exception:
            pass
        seek_time = max(duration / 2, 1) if duration > 2 else 0

        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-y', '-ss', str(seek_time), '-i', filepath,
            '-vframes', '1', '-q:v', '2',
            '-vf', 'scale=320:-1',
            thumb_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.communicate()
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception:
        pass
    return None


async def send_file_with_progress(
    client, chat_id: int, filepath: str, caption: str,
    status_msg: Message, buttons=None, supports_streaming: bool = True
):
    file_size = os.path.getsize(filepath)
    start_time = time.time()
    last_update = [0.0]
    last_bytes = [0]
    last_time = [start_time]

    # اطلاعات ویدیو برای نمایش درست در تلگرام
    duration, width, height = await get_video_info(filepath)
    thumb_path = await get_video_thumbnail(filepath)

    async def progress_cb(current: int, total: int):
        now = time.time()
        if now - last_update[0] < 1.5 and current != total:
            return
        last_update[0] = now
        dt = now - last_time[0]
        speed = (current - last_bytes[0]) / dt if dt > 0 else 0
        last_bytes[0] = current
        last_time[0] = now
        text = build_progress_text("📤 Uploading", current, total, speed, start_time)
        try:
            await status_msg.edit(text, parse_mode='markdown')
        except Exception: pass

    try:
        duration_int = int(duration) if duration else 0
        await client.send_file(
            chat_id, filepath, caption=caption,
            supports_streaming=True, buttons=buttons, parse_mode='markdown',
            progress_callback=progress_cb,
            attributes=[
                DocumentAttributeVideo(
                    duration=duration_int,
                    w=width if width else 0,
                    h=height if height else 0,
                    supports_streaming=True,
                )
            ],
            thumb=thumb_path,
        )
    finally:
        # پاک کردن thumbnail موقت
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass

    try:
        await status_msg.delete()
    except Exception: pass


# ====================== DOWNLOAD AND SEND ======================
async def do_download_and_send(event, status_msg, direct_url: str, source_url: str, extra_headers: Optional[dict] = None):
    dl_id = f"dl_{event.chat_id}_{event.id}_{int(time.time())}"
    active_downloads[dl_id] = {"paused": False, "cancelled": False}

    filepath, dl_error, final_size = await download_with_controls(
        direct_url, status_msg, dl_id, referer=source_url, extra_headers=extra_headers
    )

    # FIX: 403 → auto-retry via dirpy
    if dl_error == "HTTP_403":
        await safe_edit(status_msg, "🔄 403 received — extracting via Dirpy...")
        found_urls, session_headers, intercept_err = await extract_video_url_smart(source_url, status_msg)
        if not found_urls:
            await safe_edit(status_msg, f"❌ Could not extract via Dirpy either:\n{intercept_err}")
            return
        direct_url = found_urls[0]
        extra_headers = session_headers
        dl_id2 = f"dl_{event.chat_id}_{event.id}_{int(time.time())}_r"
        active_downloads[dl_id2] = {"paused": False, "cancelled": False}
        filepath, dl_error, final_size = await download_with_controls(
            direct_url, status_msg, dl_id2, referer=source_url, extra_headers=extra_headers
        )

    if dl_error or not filepath:
        if dl_error != "Cancelled by user":
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
        return

    video_id = f"vid_{event.chat_id}_{int(time.time())}"
    video_cache[video_id] = {
        "filepath": filepath, "chat_id": event.chat_id,
        "original_size": final_size, "original_url": source_url
    }
    after_buttons = [
        [Button.inline("🗜 Compress Video", f"compress_{video_id}")],
        [Button.inline("✅ Check (Delete)", f"check_{video_id}")]
    ]
    await safe_edit(status_msg, "📤 Uploading...")
    try:
        # مدت زمان ویدیو رو برای caption بگیر
        vid_duration, _, _ = await get_video_info(filepath)
        dur_str = ""
        if vid_duration and vid_duration > 0:
            mins, secs = divmod(int(vid_duration), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                dur_str = f"\n⏱ Duration: {hours}:{mins:02d}:{secs:02d}"
            else:
                dur_str = f"\n⏱ Duration: {mins}:{secs:02d}"

        await send_file_with_progress(
            client=event.client, chat_id=event.chat_id, filepath=filepath,
            caption=(
                f"🎬 **Video Downloaded**\n"
                f"📦 Size: {human_readable_size(final_size)}"
                f"{dur_str}\n"
                f"🔗 [Source]({source_url})\n"
                f"⬇️ [DW Link]({direct_url})"
            ),
            status_msg=status_msg, buttons=after_buttons, supports_streaming=True
        )
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")


# ====================== GET FILE SIZE ======================
async def get_file_size(url: str) -> int:
    try:
        timeout = ClientTimeout(connect=10, sock_read=10, total=15)
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(url, headers=headers, allow_redirects=True, ssl=False) as resp:
                return int(resp.headers.get('content-length', 0))
    except Exception:
        return 0


def _url_label(url: str, size: int, index: int) -> str:
    u = url.lower()
    quality = "Unknown"
    for q in ['2160p', '1080p', '720p', '480p', '360p', '240p', '4k', 'hd', 'sd']:
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


# ====================== VIDEO URL EXTRACTOR ======================
SKIP_KEYWORDS = ['thumb', 'preview', 'poster', 'banner', 'logo', 'icon', 'sprite',
                 'storyboard', 'tracking', 'analytics', 'pixel', 'ad/', '/ads/']
MIN_SIZE = 2 * 1024 * 1024  # 2MB

KNOWN_CDN_DOMAINS = [
    'rdtcdn.com', 'phncdn.com', 'xnxx-cdn.com',
    'media4.luxuretv', 'media.luxuretv',
    'rule34.xxx', 'rule34video',
    'kv-ph.', 'ev-ph.', 'di-ph.',
    'googlevideo.com', 'videoplayback',
    'p300cdn', 'x-tg.tube/get_file',
]


def _should_capture(url: str, content_type: str = "", content_length: int = 0) -> bool:
    ul = url.lower()
    if any(k in ul for k in SKIP_KEYWORDS):
        return False
    if 'video/' in content_type and content_length > MIN_SIZE:
        return True
    is_known_cdn = any(d in ul for d in KNOWN_CDN_DOMAINS)
    has_video_ext = '.mp4' in ul or '.webm' in ul or 'videoplayback' in ul or '/get_file/' in ul
    if is_known_cdn and has_video_ext:
        if 'rdtcdn.com' in ul or 'phncdn.com' in ul:
            quality_signals = [
                '_720p_', '_1080p_', '_480p_', '_240p_', '_2160p_',
                '_4000k_', '_2000k_', '_1000k_', '_500k_', '_800k_',
                'p_720', 'p_1080', 'p_480', 'p_240',
            ]
            return any(q in ul for q in quality_signals)
        return True
    return False


def _extract_from_html(html: str, seen: set, captured_urls: list, label: str):
    for m in re.findall(r"https?://[^\x22\x27<>\s]+", html):
        if _should_capture(m):
            norm = m.split('?')[0]
            if norm not in seen:
                seen.add(norm)
                captured_urls.append(m)
                logger.info(f"[{label}-URL] {m[:180]}")

    kv_patterns = [
        r"video_url\s*:\s*[\x22\x27](?:function/\d+/)?(https?://[^\x22\x27\s]+)[\x22\x27]",
        r"video_url_hd\s*:\s*[\x22\x27](?:function/\d+/)?(https?://[^\x22\x27\s]+)[\x22\x27]",
        r"event_reporting2\s*:\s*[\x22\x27]([^\x22\x27\s]+/get_file/[^\x22\x27\s]+)[\x22\x27]",
        r"[\x22\x27](?:file|src|url|video_url)[\x22\x27\s]*:\s*[\x22\x27](?:function/\d+/)?(https?://[^\x22\x27\s]+\.mp4[^\x22\x27\s]*)[\x22\x27]",
    ]
    for pat in kv_patterns:
        for m in re.findall(pat, html, re.IGNORECASE):
            url = m.rstrip('/')
            if not url.startswith('http'):
                continue
            norm = url.split('?')[0]
            if norm not in seen and not any(k in url.lower() for k in SKIP_KEYWORDS):
                seen.add(norm)
                captured_urls.append(url)
                logger.info(f"[{label}-KV] {url[:180]}")

    for m in re.findall(r"[\x22\x27]([^\x22\x27]*?/get_file/[^\x22\x27]+\.mp4[^\x22\x27]*)[\x22\x27]", html):
        if m.startswith('http'):
            url = m.rstrip('/')
            norm = url.split('?')[0]
            if norm not in seen:
                seen.add(norm)
                captured_urls.append(url)
                logger.info(f"[{label}-GETFILE] {url[:180]}")


async def _collect_from_page(page, label: str, captured_urls: list, seen: set):
    async def on_response(response):
        try:
            ct = response.headers.get('content-type', '')
            cl = int(response.headers.get('content-length', 0))
            ru = response.url
            if _should_capture(ru, ct, cl):
                norm = ru.split('?')[0]
                if norm not in seen:
                    seen.add(norm)
                    captured_urls.append(ru)
                    logger.info(f"[{label}] {ru[:180]}")
        except Exception:
            pass
    page.on('response', on_response)

    try:
        html = await page.content()
        _extract_from_html(html, seen, captured_urls, label + "-FAST")
    except Exception:
        pass

    if not captured_urls:
        await page.wait_for_timeout(5000)
        try:
            html = await page.content()
            _extract_from_html(html, seen, captured_urls, label + "-AFTER5S")
        except Exception:
            pass

    if not captured_urls:
        await page.evaluate('() => { try { document.querySelector("video")?.play(); } catch(e){} }')
        await page.wait_for_timeout(6000)
        try:
            html = await page.content()
            _extract_from_html(html, seen, captured_urls, label + "-AFTERPLAY")
        except Exception:
            pass


async def extract_video_url_smart(video_url: str, status_msg: Message) -> Tuple[list, dict, Optional[str]]:
    async with async_playwright() as p:
        browser = None
        captured_urls: list = []
        seen: set = set()
        session_headers: dict = {}

        try:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )

            async def make_context():
                return await browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                    viewport={'width': 1280, 'height': 720}
                )

            # مرحله ۱: Dirpy
            await safe_edit(status_msg, "🔗 Opening Dirpy Studio...")
            ctx1 = await make_context()
            page1 = await ctx1.new_page()
            dirpy_url = f"https://dirpy.com/studio?url={quote(video_url)}"
            try:
                await page1.goto(dirpy_url, wait_until='domcontentloaded', timeout=60000)
                await _collect_from_page(page1, "DIRPY", captured_urls, seen)
                if captured_urls:
                    session_headers = {"Referer": video_url}
            except Exception as e:
                logger.warning(f"Dirpy page error: {e}")
            finally:
                await page1.close()
                await ctx1.close()

            # مرحله ۲: Direct site fallback
            if not captured_urls:
                await safe_edit(status_msg, "🌐 Dirpy failed — trying direct site extraction...")
                ctx2 = await make_context()
                page2 = await ctx2.new_page()
                try:
                    async def handle_dialog(dialog):
                        await dialog.accept()
                    page2.on('dialog', handle_dialog)

                    await page2.goto(video_url, wait_until='domcontentloaded', timeout=60000)

                    age_selectors = [
                        'button:has-text("I AM 18")', 'button:has-text("ENTER")',
                        'button:has-text("Yes")', '.age-gate button', 'button.y',
                        'button:has-text("Enter")', 'button:has-text("Confirm")',
                        'a:has-text("I AM 18")', 'a:has-text("ENTER")',
                    ]
                    for sel in age_selectors:
                        try:
                            el = page2.locator(sel).first
                            if await el.is_visible(timeout=800):
                                await el.click()
                                await asyncio.sleep(1.5)
                                break
                        except Exception:
                            continue

                    await _collect_from_page(page2, "DIRECT", captured_urls, seen)

                    if captured_urls:
                        raw_cookies = await ctx2.cookies()
                        cookie_str = "; ".join(
                            f"{c['name']}={c['value']}" for c in raw_cookies
                            if video_url.split('/')[2].replace('www.', '') in c.get('domain', '')
                               or c.get('domain', '').lstrip('.') in video_url
                        )
                        session_headers = {
                            "Referer": video_url,
                            "Origin": '/'.join(video_url.split('/')[:3]),
                        }
                        if cookie_str:
                            session_headers["Cookie"] = cookie_str

                except Exception as e:
                    logger.warning(f"Direct page error: {e}")
                finally:
                    await page2.close()
                    await ctx2.close()

            if captured_urls:
                return captured_urls, session_headers, None
            return [], {}, "Could not capture video link via Dirpy or direct extraction"

        except Exception as e:
            logger.error(f"Extractor error: {e}")
            return [], {}, str(e)
        finally:
            if browser:
                await browser.close()


# ====================== HTML TO PDF ======================
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
                for sel in ['button:has-text("I AM 18")', 'button:has-text("ENTER")',
                            'button:has-text("Yes")', '.age-gate button', 'button.y']:
                    try:
                        el = page.locator(sel).first
                        if await el.is_visible(timeout=1000):
                            await el.click()
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
            await page.pdf(path=filepath, format="A4", print_background=True,
                           margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"})
            return filepath, None, os.path.getsize(filepath)
        except Exception as e:
            return None, f"PDF Error: {str(e)[:80]}", 0
        finally:
            if browser:
                await browser.close()


# ====================== CAPTURE MHTML ======================
async def capture_mhtml(url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str], int]:
    async with async_playwright() as p:
        browser = None
        try:
            await safe_edit(status_msg, "🌐 Capturing full webpage as MHTML...")
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(3)
            cdp = await context.new_cdp_session(page)
            result = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            mhtml_data = result.get("data", "")
            if not mhtml_data:
                return None, "Failed to capture MHTML", 0
            filepath = os.path.join(OUTPUT_FOLDER, f"page_{int(time.time())}.mhtml")
            async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
                await f.write(mhtml_data)
            return filepath, None, os.path.getsize(filepath)
        except Exception as e:
            return None, f"MHTML Error: {str(e)[:80]}", 0
        finally:
            if browser:
                await browser.close()


# ====================== VIDEO COMPRESSION ======================
async def _run_ffmpeg(args: list) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    return proc.returncode, stderr.decode(errors='replace')


async def get_video_info(input_path: str) -> Tuple[Optional[float], int, int]:
    proc = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_format', '-show_streams', input_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None, 0, 0
    try:
        info = json.loads(stdout.decode())
        dur = float(info.get('format', {}).get('duration', 0))
        w, h = 0, 0
        for s in info.get('streams', []):
            if s.get('codec_type') == 'video':
                w = int(s.get('width', 0))
                h = int(s.get('height', 0))
                if not dur:
                    dur = float(s.get('duration', 0))
                break
        return dur or None, w, h
    except Exception:
        return None, 0, 0


async def compress_video(input_path: str, target_size_bytes: int, status_msg: Message) -> Tuple[Optional[str], str]:
    target_mb = target_size_bytes / 1024 / 1024
    output_path = os.path.join(OUTPUT_FOLDER, f"compressed_{int(target_mb)}mb_{int(time.time())}.mp4")
    passlog = os.path.join(OUTPUT_FOLDER, f"passlog_{int(time.time())}")

    await safe_edit(status_msg, "🔍 Analyzing video...")

    try:
        duration, width, height = await get_video_info(input_path)
        if not duration or duration <= 0:
            return None, "Could not read video duration."

        audio_bitrate_bps = 64_000 if target_size_bytes <= 20 * 1024 * 1024 else 128_000
        total_bitrate_bps = int((target_size_bytes * 8) / duration * 0.95)
        video_bitrate_bps = max(total_bitrate_bps - audio_bitrate_bps, 10_000)
        audio_bitrate_k = audio_bitrate_bps // 1000

        # FIX: scale + format=yuv420p + noautorotate
        # - format=yuv420p: مطمئن میشه pixel format با libx264 سازگاره
        # - noautorotate: جلوگیری از تداخل rotation metadata با scale filter
        SCALE_VF = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
        COMMON_INPUT = ['-noautorotate', '-i', input_path]

        await safe_edit(
            status_msg,
            f"⚙️ Compressing to ≈ {human_readable_size(target_size_bytes)}\n"
            f"📊 Duration: {int(duration)}s  |  Video: {video_bitrate_bps//1000}kbps\n"
            f"🔄 Pass 1/2..."
        )

        pass1_args = [
            'ffmpeg', '-y', *COMMON_INPUT,
            '-vf', SCALE_VF,
            '-c:v', 'libx264', '-b:v', str(video_bitrate_bps),
            '-pass', '1', '-passlogfile', passlog,
            '-an', '-f', 'null', '/dev/null'
        ]
        rc, err = await _run_ffmpeg(pass1_args)

        if rc != 0:
            logger.warning(f"Two-pass pass1 failed → single-pass CRF. err: {err[:200]}")
            await safe_edit(status_msg, "⚙️ Single-pass encoding (CRF mode)...")
            sp_args = [
                'ffmpeg', '-y', *COMMON_INPUT,
                '-vf', SCALE_VF,
                '-c:v', 'libx264', '-crf', '28',
                '-maxrate', str(video_bitrate_bps),
                '-bufsize', str(video_bitrate_bps * 2),
                '-preset', 'fast',
                '-c:a', 'aac', '-b:a', f'{audio_bitrate_k}k',
                '-movflags', '+faststart',
                output_path
            ]
            rc2, err2 = await _run_ffmpeg(sp_args)
            if rc2 != 0:
                return None, f"FFmpeg error: {err2[-300:]}"
        else:
            await safe_edit(
                status_msg,
                f"⚙️ Compressing to ≈ {human_readable_size(target_size_bytes)}\n"
                f"📊 Duration: {int(duration)}s  |  Video: {video_bitrate_bps//1000}kbps\n"
                f"🔄 Pass 2/2..."
            )
            pass2_args = [
                'ffmpeg', '-y', *COMMON_INPUT,
                '-vf', SCALE_VF,
                '-c:v', 'libx264', '-b:v', str(video_bitrate_bps),
                '-pass', '2', '-passlogfile', passlog,
                '-preset', 'fast',
                '-c:a', 'aac', '-b:a', f'{audio_bitrate_k}k',
                '-movflags', '+faststart',
                output_path
            ]
            rc, err = await _run_ffmpeg(pass2_args)
            if rc != 0:
                return None, f"FFmpeg pass2 error: {err[-300:]}"

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return None, "Output file is empty or missing."

        return output_path, f"✅ Compressed: {human_readable_size(os.path.getsize(output_path))}"

    except FileNotFoundError:
        return None, "ffmpeg/ffprobe not found. Please install ffmpeg on the server."
    except Exception as e:
        logger.error(f"Compression error: {e}", exc_info=True)
        return None, f"Unexpected error: {str(e)[:150]}"
    finally:
        for ext in ['.log', '-0.log', '-0.log.mbtree']:
            try:
                pp = passlog + ext
                if os.path.exists(pp): os.remove(pp)
            except Exception: pass


# ====================== DIRPY FLOW ======================
processing_messages = set()

async def process_dirpy_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    status_msg = await event.reply("🔄 Starting extraction...", parse_mode='markdown')
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        found_urls, session_headers, intercept_err = await extract_video_url_smart(url, status_msg)
        if not found_urls:
            await safe_edit(status_msg, f"❌ Could not capture video:\n{intercept_err}")
            return
        if len(found_urls) == 1:
            await do_download_and_send(event, status_msg, found_urls[0], url, extra_headers=session_headers)
            return
        await safe_edit(status_msg, f"🔍 Found {len(found_urls)} links, checking sizes...")
        sized_urls = []
        for u in found_urls:
            sz = await get_file_size(u)
            sized_urls.append((u, sz))
        pick_id = f"pick_{event.chat_id}_{int(time.time())}"
        video_cache[pick_id] = {
            "urls": sized_urls, "source_url": url,
            "chat_id": event.chat_id, "session_headers": session_headers
        }
        buttons = [[Button.inline(_url_label(u, sz, i), f"pickurl_{pick_id}_{i}")] for i, (u, sz) in enumerate(sized_urls)]
        await safe_edit(status_msg, "📋 **Select video to download:**")
        await event.client.send_message(
            event.chat_id,
            f"🎬 Found **{len(sized_urls)}** video links.\nChoose one to download:",
            buttons=buttons, parse_mode='markdown'
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
    video_id = event.data.decode().replace("compress_", "")
    if video_id not in video_cache:
        return await event.answer("Video not found or expired.", alert=True)
    await event.answer("Send desired size (e.g: 15mb or 800kb)", alert=False)
    # FIX: chat_id رو ذخیره میکنیم (نه sender_id) — در private chat یکیه ولی در گروه فرق دارن
    user_state[event.chat_id] = {"action": "wait_for_compression_size", "video_id": video_id}


@events.register(events.CallbackQuery(pattern=r"check_(.+)"))
async def check_callback(event):
    video_id = event.data.decode().replace("check_", "")
    if video_id not in video_cache:
        return await event.answer("Video already deleted.", alert=True)
    data = video_cache[video_id]
    try:
        if os.path.exists(data["filepath"]): os.remove(data["filepath"])
        await event.answer("✅ Video deleted from server.", alert=False)
        await event.edit(buttons=None)
    except Exception:
        await event.answer("Error deleting file.", alert=True)
    video_cache.pop(video_id, None)


@events.register(events.CallbackQuery(pattern=r"pickurl_(.+)_(\d+)$"))
async def pickurl_callback(event):
    parts = event.data.decode().rsplit("_", 1)
    idx = int(parts[1])
    pick_id = parts[0].replace("pickurl_", "")
    if pick_id not in video_cache:
        return await event.answer("Session expired. Please resend /dirpy command.", alert=True)
    data = video_cache[pick_id]
    if idx >= len(data["urls"]):
        return await event.answer("Invalid selection.", alert=True)
    chosen_url, _ = data["urls"][idx]
    source_url = data["source_url"]
    session_headers = data.get("session_headers", {})
    await event.answer(f"Starting download #{idx+1}...", alert=False)
    try:
        await event.delete()
    except Exception: pass
    status_msg = await event.client.send_message(event.chat_id, "📥 Starting download...")
    del video_cache[pick_id]
    await do_download_and_send(event, status_msg, chosen_url, source_url, extra_headers=session_headers)


# ====================== ADMIN HANDLERS ======================
@events.register(events.NewMessage(incoming=True))
async def admin_input_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    if event.sender_id not in admin_pending_add:
        return
    action = admin_pending_add.pop(event.sender_id)
    raw = event.raw_text.strip()
    if not raw.isdigit():
        await event.reply("❌ Invalid ID! Please send a numeric ID only.", parse_mode='markdown')
        raise events.StopPropagation
    uid = int(raw)
    if action == "add":
        if uid in AUTHORIZED_USERS:
            await event.reply(f"⚠️ User `{uid}` is already authorized.", parse_mode='markdown')
        else:
            AUTHORIZED_USERS.add(uid)
            await event.reply(f"✅ User `{uid}` added!\nTotal: **{len(AUTHORIZED_USERS)}**", parse_mode='markdown')
    elif action == "remove":
        if uid == ADMIN_ID:
            await event.reply("❌ You cannot remove yourself!", parse_mode='markdown')
        elif uid not in AUTHORIZED_USERS:
            await event.reply(f"⚠️ User `{uid}` not found.", parse_mode='markdown')
        else:
            AUTHORIZED_USERS.discard(uid)
            await event.reply(f"✅ User `{uid}` removed!\nTotal: **{len(AUTHORIZED_USERS)}**", parse_mode='markdown')
    raise events.StopPropagation


# ====================== SIZE INPUT HANDLER ======================
@events.register(events.NewMessage(incoming=True))
async def size_input_handler(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    # FIX: chat_id (نه sender_id) — اصلاح اصلی برای "Invalid size format" bug
    state = user_state.get(event.chat_id)
    if not state or state.get("action") != "wait_for_compression_size":
        return

    video_id = state["video_id"]
    if video_id not in video_cache:
        user_state.pop(event.chat_id, None)
        raise events.StopPropagation

    target_bytes = parse_size_input(event.raw_text)
    if not target_bytes:
        await event.reply(
            "❌ Invalid size format!\nExamples: `15mb`, `800kb`, `1.5gb`",
            parse_mode='markdown'
        )
        raise events.StopPropagation

    data = video_cache[video_id]
    if target_bytes >= data["original_size"]:
        await event.reply("❌ Target size must be smaller than original size.", parse_mode='markdown')
        raise events.StopPropagation

    # state رو قبل از شروع پاک کن — جلوگیری از double-trigger
    user_state.pop(event.chat_id, None)

    status_msg = await event.reply(f"⚙️ Starting compression → {human_readable_size(target_bytes)}...")
    compressed_path, result = await compress_video(data["filepath"], target_bytes, status_msg)

    if compressed_path and os.path.exists(compressed_path):
        await safe_edit(status_msg, "📤 Uploading compressed video...")
        try:
            await send_file_with_progress(
                client=event.client, chat_id=event.chat_id, filepath=compressed_path,
                caption=(
                    f"✅ **Compressed Video**\n"
                    f"🎯 Requested: {human_readable_size(target_bytes)}\n"
                    f"📦 Final Size: {human_readable_size(os.path.getsize(compressed_path))}"
                ),
                status_msg=status_msg,
            )
        except Exception as e:
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
        try:
            os.remove(compressed_path)
            os.remove(data["filepath"])
        except Exception: pass
    else:
        await safe_edit(status_msg, f"❌ Compression failed: {result}")

    video_cache.pop(video_id, None)
    raise events.StopPropagation


# ====================== PDF & HTML COMMANDS ======================
async def process_pdf_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages: return
    processing_messages.add(msg_id)
    status = await event.reply("📄 Converting to PDF...", parse_mode='markdown')
    filepath = None
    try:
        if not url.startswith(('http://', 'https://')): url = 'https://' + url
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
            if filepath and os.path.exists(filepath): os.remove(filepath)
        except Exception: pass


async def process_html_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages: return
    processing_messages.add(msg_id)
    status = await event.reply("🌐 Capturing full webpage...", parse_mode='markdown')
    filepath = None
    try:
        if not url.startswith(('http://', 'https://')): url = 'https://' + url
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
            if filepath and os.path.exists(filepath): os.remove(filepath)
        except Exception: pass


# ====================== TELEGRAM COMMANDS ======================
@events.register(events.NewMessage(pattern='/admin', incoming=True))
async def admin_cmd(event):
    if event.sender_id != ADMIN_ID:
        return await event.reply("⛔ Unauthorized")
    users_list = "\n".join([f"• `{uid}`" for uid in sorted(AUTHORIZED_USERS)])
    await event.reply(
        f"👑 **Admin Panel**\n\n**Authorized Users ({len(AUTHORIZED_USERS)}):**\n{users_list}\n\nChoose an action:",
        parse_mode='markdown',
        buttons=[
            [Button.inline("➕ Add User", "admin_add")],
            [Button.inline("➖ Remove User", "admin_remove")],
            [Button.inline("🔄 Refresh List", "admin_refresh")],
        ]
    )


@events.register(events.CallbackQuery(pattern=r"admin_add"))
async def admin_add_callback(event):
    if event.sender_id != ADMIN_ID: return await event.answer("Unauthorized", alert=True)
    admin_pending_add[event.sender_id] = "add"
    await event.answer("", alert=False)
    await event.client.send_message(event.chat_id, "📩 Send me the **numeric user ID** to add:", parse_mode='markdown',
        buttons=[[Button.inline("❌ Cancel", "admin_cancel")]])


@events.register(events.CallbackQuery(pattern=r"admin_remove"))
async def admin_remove_callback(event):
    if event.sender_id != ADMIN_ID: return await event.answer("Unauthorized", alert=True)
    admin_pending_add[event.sender_id] = "remove"
    await event.answer("", alert=False)
    await event.client.send_message(event.chat_id, "📩 Send me the **numeric user ID** to remove:", parse_mode='markdown',
        buttons=[[Button.inline("❌ Cancel", "admin_cancel")]])


@events.register(events.CallbackQuery(pattern=r"admin_refresh"))
async def admin_refresh_callback(event):
    if event.sender_id != ADMIN_ID: return await event.answer("Unauthorized", alert=True)
    users_list = "\n".join([f"• `{uid}`" for uid in sorted(AUTHORIZED_USERS)])
    await event.answer("✅ Refreshed", alert=False)
    try:
        await event.edit(
            f"👑 **Admin Panel**\n\n**Authorized Users ({len(AUTHORIZED_USERS)}):**\n{users_list}\n\nChoose an action:",
            parse_mode='markdown',
            buttons=[
                [Button.inline("➕ Add User", "admin_add")],
                [Button.inline("➖ Remove User", "admin_remove")],
                [Button.inline("🔄 Refresh List", "admin_refresh")],
            ]
        )
    except Exception: pass


@events.register(events.CallbackQuery(pattern=r"admin_cancel"))
async def admin_cancel_callback(event):
    if event.sender_id != ADMIN_ID: return await event.answer("Unauthorized", alert=True)
    admin_pending_add.pop(event.sender_id, None)
    await event.answer("Cancelled", alert=False)
    try: await event.delete()
    except Exception: pass


@events.register(events.NewMessage(pattern='/start', incoming=True))
async def start_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    await event.reply(
        "🚀 **Ultimate Bot v5**\n\n"
        "• `/dirpy <url>` → Download video\n"
        "• `/pdf <url>` → Webpage to PDF\n"
        "• `/html <url>` → Save as MHTML\n\n"
        "**During download:** ⏸ Pause  •  ❌ Cancel\n"
        "**After download:** 🗜 Compress  •  ✅ Delete",
        parse_mode='markdown'
    )


@events.register(events.NewMessage(pattern='/dirpy', incoming=True))
async def dirpy_command(event):
    if event.sender_id not in AUTHORIZED_USERS: return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2: return await event.reply("❌ Usage: `/dirpy <url>`", parse_mode='markdown')
    await process_dirpy_request(event, parts[1].strip())


@events.register(events.NewMessage(pattern='/pdf', incoming=True))
async def pdf_command(event):
    if event.sender_id not in AUTHORIZED_USERS: return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2: return await event.reply("❌ Usage: `/pdf <url>`", parse_mode='markdown')
    await process_pdf_request(event, parts[1].strip())


@events.register(events.NewMessage(pattern='/html', incoming=True))
async def html_command(event):
    if event.sender_id not in AUTHORIZED_USERS: return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2: return await event.reply("❌ Usage: `/html <url>`", parse_mode='markdown')
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
    target_url = urls[0]
    dl_id = f"dl_{event.chat_id}_{event.id}_{int(time.time())}"
    active_downloads[dl_id] = {"paused": False, "cancelled": False}
    status_msg = await event.reply("⏬ Downloading...")

    filepath, error, size = await download_with_controls(target_url, status_msg, dl_id, referer=target_url)

    # FIX: 403 → auto-dirpy
    if error == "HTTP_403":
        await safe_edit(status_msg, "🔄 403 — extracting via Dirpy...")
        await process_dirpy_request(event, target_url)
        try: await status_msg.delete()
        except Exception: pass
        return

    if error or not filepath:
        if error != "Cancelled by user":
            await safe_edit(status_msg, f"❌ {error or 'Failed'}")
        return
    await safe_edit(status_msg, "📤 Uploading...")
    try:
        vid_duration, _, _ = await get_video_info(filepath)
        dur_str = ""
        if vid_duration and vid_duration > 0:
            mins, secs = divmod(int(vid_duration), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                dur_str = f" | ⏱ {hours}:{mins:02d}:{secs:02d}"
            else:
                dur_str = f" | ⏱ {mins}:{secs:02d}"
        await send_file_with_progress(
            client=event.client, chat_id=event.chat_id, filepath=filepath,
            caption=f"📦 {human_readable_size(size)}{dur_str}", status_msg=status_msg,
        )
    except Exception as e:
        await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
        return
    try: os.remove(filepath)
    except Exception: pass


# ====================== MAIN ======================
async def main():
    print("\n" + "="*60)
    print("🚀 ULTIMATE BOT v5")
    print("   FIX 1: 403 → auto-retry via Dirpy")
    print("   FIX 2: FFmpeg -noautorotate + yuv420p")
    print("   FIX 3: size_input uses chat_id (not sender_id)")
    print("   FIX 4: pause/resume split callbacks")
    print("="*60)

    start_keep_alive()
    client = TelegramClient('ultimate_bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    client.add_event_handler(admin_cmd)
    client.add_event_handler(admin_add_callback)
    client.add_event_handler(admin_remove_callback)
    client.add_event_handler(admin_refresh_callback)
    client.add_event_handler(admin_cancel_callback)
    client.add_event_handler(admin_input_handler)
    client.add_event_handler(start_cmd)
    client.add_event_handler(dirpy_command)
    client.add_event_handler(pdf_command)
    client.add_event_handler(html_command)
    client.add_event_handler(compress_callback)
    client.add_event_handler(check_callback)
    client.add_event_handler(pickurl_callback)
    client.add_event_handler(dl_pause_callback)
    client.add_event_handler(dl_resume_callback)
    client.add_event_handler(dl_cancel_callback)
    client.add_event_handler(size_input_handler)
    client.add_event_handler(generic_url_handler)

    me = await client.get_me()
    logger.info(f"✅ Bot started as @{me.username}")
    print(f"✅ Bot is online → @{me.username}")
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        import uvloop
        uvloop.run(main())
    except ImportError:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
