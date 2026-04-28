#!/usr/bin/env python3
# Telegram Ultimate Bot - FIXED VERSION (PDF + LuxureTV + Check Button)

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


# ====================== SMART INTERCEPTION (بهبود یافته برای LuxureTV) ======================
async def extract_video_url_smart(video_url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str]]:
    async with async_playwright() as p:
        browser = None
        captured_url: Optional[str] = None

        try:
            await status_msg.edit("🌐 Launching browser for network interception...")

            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--no-first-run']
            )
            page = await browser.new_page()

            async def handle_route(route):
                nonlocal captured_url
                req_url = route.request.url.lower()
                if '.mp4' in req_url and any(domain in req_url for domain in [
                    'luxuretv.com', 'xnxx-cdn.com', 'rule34.xxx', 'ahrimp4', 'media4'
                ]):
                    if not captured_url:
                        captured_url = route.request.url
                        logger.info(f"✅ Captured MP4: {req_url[:180]}")
                await route.continue_()

            await page.route("**/*", handle_route)

            dirpy_url = f"https://dirpy.com/studio?url={quote(video_url)}"
            await page.goto(dirpy_url, wait_until="domcontentloaded", timeout=50000)

            await page.wait_for_timeout(18000)   # افزایش زمان صبر برای LuxureTV

            if not captured_url:
                await page.evaluate('() => { const videos = document.querySelectorAll("video"); if(videos.length) videos[0].play(); }')
                await page.wait_for_timeout(12000)

            if captured_url:
                return captured_url, None
            return None, "Could not find direct MP4 link"

        except Exception as e:
            logger.error(f"Interception error: {e}")
            return None, str(e)
        finally:
            if browser:
                await browser.close()


# ====================== PDF FIXED (کمتر گیر کردن) ======================
async def html_to_pdf(url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str], int]:
    async with async_playwright() as p:
        browser = None
        try:
            await status_msg.edit("📄 Converting to PDF...")
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = await browser.new_page()
            await page.goto(url, wait_until="load", timeout=60000)   # از networkidle به load تغییر دادیم
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(4)

            filepath = os.path.join(OUTPUT_FOLDER, f"pdf_{int(time.time())}.pdf")
            await page.pdf(path=filepath, format='A4', print_background=True)
            size = os.path.getsize(filepath)
            return filepath, None, size
        except Exception as e:
            logger.error(f"PDF Error: {e}")
            return None, f"PDF timeout or error: {str(e)[:80]}", 0
        finally:
            if browser:
                await browser.close()
                # ====================== VIDEO COMPRESSION ======================
async def compress_video(input_path: str, target_size_bytes: int, status_msg: Message) -> Tuple[Optional[str], str]:
    output_path = input_path.replace(".mp4", f"_compressed_{int(target_size_bytes/1024/1024)}mb.mp4")

    await safe_edit(status_msg, f"⚙️ Compressing video to ≈ {human_readable_size(target_size_bytes)}...")

    try:
        cmd_duration = f"ffprobe -v quiet -print_format json -show_format \"{input_path}\""
        proc = await asyncio.create_subprocess_shell(cmd_duration, stdout=asyncio.subprocess.PIPE)
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

        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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


# ====================== PROCESS DIRPY REQUEST (مستقیم Dirpy) ======================
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

        await safe_edit(status_msg, "🌐 Opening Dirpy Studio and capturing video stream...")

        direct_url, intercept_err = await extract_video_url_smart(url, status_msg)

        if intercept_err or not direct_url:
            await safe_edit(status_msg, f"❌ Could not capture video link:\n{intercept_err}")
            return

        await safe_edit(status_msg, "📥 Direct link found. Downloading video...")

        filepath, dl_error, final_size = await download_direct_with_progress(direct_url, status_msg, referer=url)

        if dl_error or not filepath:
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
            return

        # ذخیره برای فشرده‌سازی
        video_id = f"vid_{event.chat_id}_{int(time.time())}"
        video_cache[video_id] = {
            "filepath": filepath,
            "chat_id": event.chat_id,
            "original_size": final_size,
            "original_url": url
        }

        # دو دکمه: Compress و Check (حذف)
        buttons = [
            [Button.inline("🗜 Compress Video", f"compress_{video_id}")],
            [Button.inline("✅ Check (Delete from server)", f"check_{video_id}")]
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
        await safe_edit(status_msg, f"❌ Unexpected error: {str(e)[:100]}")
    finally:
        processing_messages.discard(msg_id)


# ====================== CALLBACK HANDLERS ======================
@events.register(events.CallbackQuery(pattern=r"compress_(.+)"))
async def compress_callback(event):
    video_id = event.pattern_match.group(1)
    if video_id not in video_cache:
        return await event.answer("Video not found.", alert=True)

    await event.answer("Send desired size (e.g: 15mb or 800kb)", alert=False)
    user_state[event.chat_id] = {"action": "wait_for_compression_size", "video_id": video_id}


@events.register(events.CallbackQuery(pattern=r"check_(.+)"))
async def check_callback(event):
    video_id = event.pattern_match.group(1)
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
        await event.answer("Error while deleting file.", alert=True)

    if video_id in video_cache:
        del video_cache[video_id]


# ====================== SIZE INPUT HANDLER ======================
@events.register(events.NewMessage(incoming=True))
async def size_input_handler(event):
    if event.chat_id not in user_state:
        return

    state = user_state.get(event.chat_id)
    if state.get("action") != "wait_for_compression_size":
        return

    video_id = state["video_id"]
    if video_id not in video_cache:
        del user_state[event.chat_id]
        return

    target_bytes = parse_size_input(event.raw_text)
    if not target_bytes:
        await event.reply("❌ Invalid size!\nUse: `15mb`, `800kb`, `1.5gb`", parse_mode='markdown')
        return

    data = video_cache[video_id]
    if target_bytes >= data["original_size"]:
        await event.reply("❌ Target size must be smaller than original.", parse_mode='markdown')
        return

    status_msg = await event.reply(f"⚙️ Compressing to {human_readable_size(target_bytes)}...")

    compressed_path, result = await compress_video(data["filepath"], target_bytes, status_msg)

    if compressed_path and os.path.exists(compressed_path):
        await event.client.send_file(
            event.chat_id,
            compressed_path,
            caption=f"✅ **Compressed Video**\n"
                    f"🎯 Requested: {human_readable_size(target_bytes)}\n"
                    f"📦 Final: {human_readable_size(os.path.getsize(compressed_path))}",
            supports_streaming=True,
            parse_mode='markdown'
        )
        await status_msg.delete()

        try:
            os.remove(compressed_path)
            os.remove(data["filepath"])
        except:
            pass
    else:
        await safe_edit(status_msg, f"❌ Compression failed: {result}")

    # پاکسازی
    if event.chat_id in user_state:
        del user_state[event.chat_id]
    if video_id in video_cache:
        del video_cache[video_id]


# ====================== PDF & HTML ======================
async def process_pdf_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages: return
    processing_messages.add(msg_id)

    status = await event.reply("📄 Converting to PDF...", parse_mode='markdown')

    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        filepath, error, size = await html_to_pdf(url, status)
        if error:
            await safe_edit(status, f"❌ {error}")
            return
        await event.client.send_file(event.chat_id, filepath, caption=f"📑 PDF • {human_readable_size(size)}", force_document=True)
        await status.delete()
    finally:
        processing_messages.discard(msg_id)
        try:
            if 'filepath' in locals() and os.path.exists(filepath):
                os.remove(filepath)
        except: pass


async def process_html_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages: return
    processing_messages.add(msg_id)

    status = await event.reply("🌐 Capturing webpage...", parse_mode='markdown')

    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        filepath, error, size = await capture_mhtml(url, status)
        if error:
            await safe_edit(status, f"❌ {error}")
            return
        await event.client.send_file(event.chat_id, filepath, caption="📦 Full Webpage Snapshot (MHTML)")
        await status.delete()
    finally:
        processing_messages.discard(msg_id)
        try:
            if 'filepath' in locals() and os.path.exists(filepath):
                os.remove(filepath)
        except: pass


# ====================== MAIN ======================
async def main():
    print("\n" + "="*80)
    print("🚀 ULTIMATE BOT - FINAL FIXED VERSION")
    print("   PDF Fixed + LuxureTV Improved + Check Button")
    print("="*80)

    start_keep_alive()

    client = TelegramClient('ultimate_bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    client.add_event_handler(start_cmd)
    client.add_event_handler(dirpy_command)
    client.add_event_handler(pdf_command)
    client.add_event_handler(html_command)
    client.add_event_handler(generic_url_handler)
    client.add_event_handler(compress_callback)
    client.add_event_handler(check_callback)
    client.add_event_handler(size_input_handler)

    me = await client.get_me()
    logger.info(f"✅ Bot started as @{me.username}")

    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
