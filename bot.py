#!/usr/bin/env python3
# Telegram Ultimate Bot - FIXED VERSION
# PDF Fixed + LuxureTV Improved + Check Button + Direct Dirpy

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
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        if referer:
            headers['Referer'] = referer

        timeout = ClientTimeout(total=DOWNLOAD_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as response:
                if response.status != 200:
                    return None, f"HTTP {response.status}", 0

                cd = response.headers.get('Content-Disposition', '')
                filename = f"video_{int(time.time())}.mp4"
                if 'filename=' in cd:
                    m = re.search(r'filename="?([^";]+)', cd)
                    if m:
                        filename = m.group(1).strip()

                filepath = os.path.join(OUTPUT_FOLDER, safe_filename(filename))
                total = int(response.headers.get('content-length', 0))

                if total > MAX_FILE_SIZE_MB * 1024 * 1024:
                    return None, "File too large", 0

                progress = ProgressHandler(status_msg, total)
                downloaded = 0
                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in response.content.iter_chunked(8192):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        await progress.update(downloaded)

                await progress.finish(True, "")
                return filepath, None, downloaded
    except Exception as e:
        return None, str(e), 0


def is_video_url(url: str) -> bool:
    """Check if a request URL looks like a direct video stream."""
    u = url.lower()

    SITE_PATTERNS = [
        ('xnxx-cdn.com',         lambda u: 'mp4' in u),
        ('xnxx.com',             lambda u: 'mp4' in u and '/videos/' in u),
        ('media4.luxuretv.com',  lambda u: '.mp4' in u),
        ('luxuretv.com',         lambda u: '.mp4' in u),
        ('rule34.xxx',           lambda u: '.mp4' in u or ('video' in u and 'api' not in u)),
        ('rule34video.com',      lambda u: '.mp4' in u),
        ('ahrimp4',              lambda u: True),
        ('media4',               lambda u: '.mp4' in u),
    ]

    for domain, check in SITE_PATTERNS:
        if domain in u and check(u):
            return True

    # fallback: هر URL عمومی با .mp4 روی CDN
    if '.mp4' in u and any(cdn in u for cdn in ['-cdn', 'media', 'video', 'stream', 'content', 'storage']):
        return True

    return False


async def extract_video_url_smart(video_url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str]]:
    async with async_playwright() as p:
        browser = None
        captured_url: Optional[str] = None

        try:
            await safe_edit(status_msg, "🌐 Launching browser for network interception...")

            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )

            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1280, 'height': 720}
            )
            page = await context.new_page()

            # روش ۱: Request interception
            async def handle_route(route):
                nonlocal captured_url
                req_url = route.request.url
                if not captured_url and is_video_url(req_url):
                    captured_url = req_url
                    logger.info(f"[REQUEST] Captured: {req_url[:200]}")
                await route.continue_()

            await page.route("**/*", handle_route)

            # روش ۲: Response interception (برای سایت‌هایی که URL شامل mp4 نیست)
            async def handle_response(response):
                nonlocal captured_url
                if captured_url:
                    return
                content_type = response.headers.get('content-type', '')
                if 'video' in content_type or 'octet-stream' in content_type:
                    rurl = response.url
                    if 'dirpy.com' not in rurl.lower():
                        captured_url = rurl
                        logger.info(f"[RESPONSE content-type] Captured: {rurl[:200]}")

            page.on('response', handle_response)

            dirpy_url = f"https://dirpy.com/studio?url={quote(video_url)}"
            await safe_edit(status_msg, "🔗 Opening Dirpy Studio...")
            await page.goto(dirpy_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)

            if not captured_url:
                await safe_edit(status_msg, "⏳ Waiting for video stream...")
                # کلیک روی دکمه دانلود dirpy اگه وجود داشت
                try:
                    btn = page.locator('button:has-text("Download"), button:has-text("Get"), .btn-download, #btn-download')
                    if await btn.count() > 0:
                        await btn.first.click()
                        await page.wait_for_timeout(5000)
                except Exception:
                    pass

            if not captured_url:
                await page.evaluate(
                    '() => { const v = document.querySelector("video"); if(v) { v.muted = true; v.play(); } }'
                )
                await page.wait_for_timeout(10000)

            if not captured_url:
                # آخرین تلاش: scan کردن سورس HTML
                await safe_edit(status_msg, "🔍 Scanning page source for video links...")
                html = await page.content()
                mp4_matches = re.findall(r'https?://[^\s"\'<>]+\.mp4[^\s"\'<>]*', html)
                for match in mp4_matches:
                    if 'dirpy.com' not in match.lower():
                        captured_url = match
                        logger.info(f"[SOURCE] Captured from HTML: {match[:200]}")
                        break

            if captured_url:
                return captured_url, None
            return None, "Could not capture direct video link after all attempts"

        except Exception as e:
            logger.error(f"Interception error: {e}")
            return None, str(e)
        finally:
            if browser:
                await browser.close()


async def html_to_pdf(url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str], int]:
    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = await browser.new_page()
            await safe_edit(status_msg, "🌐 Loading page...")
            await page.goto(url, wait_until="load", timeout=60000)
            await safe_edit(status_msg, "📄 Rendering PDF...")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(4)

            filepath = os.path.join(OUTPUT_FOLDER, f"pdf_{int(time.time())}.pdf")
            await page.pdf(path=filepath, format='A4', print_background=True)
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


# ====================== PROCESS DIRPY REQUEST ======================
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

        await safe_edit(status_msg, "🌐 Opening Dirpy Studio and capturing stream...")

        direct_url, intercept_err = await extract_video_url_smart(url, status_msg)

        if intercept_err or not direct_url:
            await safe_edit(status_msg, f"❌ Could not capture video:\n{intercept_err}")
            return

        await safe_edit(status_msg, "📥 Link captured. Downloading video...")

        filepath, dl_error, final_size = await download_direct_with_progress(direct_url, status_msg, referer=url)

        if dl_error or not filepath:
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
            return

        video_id = f"vid_{event.chat_id}_{int(time.time())}"
        video_cache[video_id] = {
            "filepath": filepath,
            "chat_id": event.chat_id,
            "original_size": final_size,
            "original_url": url
        }

        buttons = [
            [Button.inline("🗜 Compress Video", f"compress_{video_id}")],
            [Button.inline("✅ Check (Delete)", f"check_{video_id}")]
        ]

        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=f"🎬 **Video Downloaded via Dirpy**\n"
                    f"📦 Size: {human_readable_size(final_size)}\n"
                    f"🔗 [Source]({url})",
            supports_streaming=True,
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

    video_id = state["video_id"]
    if video_id not in video_cache:
        user_state.pop(event.chat_id, None)
        return

    target_bytes = parse_size_input(event.raw_text)
    if not target_bytes:
        await event.reply("❌ Invalid size format!\nExamples: `15mb`, `800kb`, `1.5gb`", parse_mode='markdown')
        return

    data = video_cache[video_id]
    if target_bytes >= data["original_size"]:
        await event.reply("❌ Target size must be smaller than original size.", parse_mode='markdown')
        return

    status_msg = await event.reply(f"⚙️ Compressing to {human_readable_size(target_bytes)}...")

    compressed_path, result = await compress_video(data["filepath"], target_bytes, status_msg)

    if compressed_path and os.path.exists(compressed_path):
        await event.client.send_file(
            event.chat_id,
            compressed_path,
            caption=f"✅ **Compressed Video**\n"
                    f"🎯 Requested: {human_readable_size(target_bytes)}\n"
                    f"📦 Final Size: {human_readable_size(os.path.getsize(compressed_path))}",
            supports_streaming=True,
            parse_mode='markdown'
        )
        await status_msg.delete()

        try:
            os.remove(compressed_path)
            os.remove(data["filepath"])
        except Exception:
            pass
    else:
        await safe_edit(status_msg, f"❌ Compression failed: {result}")

    user_state.pop(event.chat_id, None)
    video_cache.pop(video_id, None)


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
    # اگه کاربر داره size می‌فرسته، این handler نباید اجرا بشه
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
    await event.client.send_file(event.chat_id, filepath, supports_streaming=True)
    await status_msg.delete()
    try:
        os.remove(filepath)
    except Exception:
        pass


# ====================== MAIN ======================
async def main():
    print("\n" + "="*80)
    print("🚀 ULTIMATE BOT - FIXED VERSION")
    print("   PDF Fixed + MHTML Added + Bracket Bug Fixed + ENV Vars")
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
