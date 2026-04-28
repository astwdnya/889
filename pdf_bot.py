#!/usr/bin/env python3
# Telegram Ultimate Bot - Direct YouTube download with yt-dlp (No 403!)

import asyncio
import os
import re
import sys
import logging
import glob
import zipfile
import time
import base64
from datetime import datetime
from urllib.parse import urlparse, quote, unquote
from html import unescape
from typing import Optional, Tuple

from flask import Flask
from threading import Thread
from telethon import TelegramClient, events, errors as telethon_errors
from telethon.tl.types import Message

import aiohttp
from aiohttp import ClientTimeout, ClientError
import aiofiles
from bs4 import BeautifulSoup
import yt_dlp

try:
    from pyppeteer import launch
    PYPPETEER_AVAILABLE = True
except ImportError:
    PYPPETEER_AVAILABLE = False
    print("⚠️ pyppeteer not installed")

# ========== CONFIGURATION ==========
BOT_TOKEN = "7675664254:AAHL7QhPonc47z0QKRFnB5p_L15SRiLBddc"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
AUTHORIZED_USERS = {818185073, 6936101187, 7972834913}

MAX_FILE_SIZE_MB = 2000
DOWNLOAD_TIMEOUT = 300

OUTPUT_FOLDER = "output_files"
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

HEALTH_PORT = int(os.environ.get('PORT', 10000))

# ========== LOGGING ==========
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== FLASK KEEP-ALIVE ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "✅ Bot is running!", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=HEALTH_PORT, debug=False, use_reloader=False)

def start_keep_alive():
    Thread(target=run_flask, daemon=True).start()
    logger.info(f"Keep-alive on port {HEALTH_PORT}")

# ========== UTILITIES ==========
def find_chromium() -> Optional[str]:
    possible_paths = [
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"
    ]
    for path in possible_paths:
        if os.path.exists(path):
            return path
    return None

def human_readable_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.2f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"

# ========== PROGRESS HANDLER ==========
class ProgressHandler:
    def __init__(self, status_message: Message, total_size: int, operation: str = "Downloading"):
        self.status_message = status_message
        self.total_size = total_size
        self.operation = operation
        self.last_update_time = 0
        self.last_bytes = 0
        self._lock = asyncio.Lock()

    async def update(self, current_bytes: int, speed_delta: Optional[float] = None):
        async with self._lock:
            now = time.time()
            if now - self.last_update_time < 1.0 and current_bytes != self.total_size:
                return

            if self.total_size > 0:
                percent = (current_bytes / self.total_size) * 100
                speed = (current_bytes - self.last_bytes) / (now - self.last_update_time) if self.last_update_time > 0 else 0
                eta = (self.total_size - current_bytes) / speed if speed > 0 else 0

                bar_length = 15
                filled = int(bar_length * current_bytes // self.total_size)
                bar = '█' * filled + '░' * (bar_length - filled)

                progress_text = (
                    f"**{self.operation}**\n"
                    f"`[{bar}]`\n"
                    f"**📦 {percent:.1f}%**\n"
                    f"📁 {human_readable_size(current_bytes)} / {human_readable_size(self.total_size)}\n"
                    f"🚀 Speed: {human_readable_size(speed)}/s\n"
                    f"⏱️ ETA: {int(eta // 60)}m {int(eta % 60)}s"
                )
            else:
                progress_text = f"**{self.operation}...**\n📥 {human_readable_size(current_bytes)} downloaded"

            try:
                await self.status_message.edit(progress_text, parse_mode='markdown')
            except telethon_errors.MessageNotModifiedError:
                pass

            self.last_update_time = now
            self.last_bytes = current_bytes

    async def finish(self, success: bool, final_message: str):
        if success:
            await self.status_message.delete()
        else:
            await self.status_message.edit(f"❌ {final_message}", parse_mode='markdown')

# ========== YT-DLP DOWNLOAD (DIRECT - NO 403!) ==========
class YTDLPProgressHook:
    def __init__(self, status_message: Message, total_size: int):
        self.status_message = status_message
        self.total_size = total_size
        self.progress = ProgressHandler(status_message, total_size, "Downloading video")
        self.downloaded = 0
    
    def hook(self, d):
        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            if downloaded > self.downloaded:
                self.downloaded = downloaded
                # Create a new asyncio task for the progress update
                asyncio.create_task(self.progress.update(downloaded))
        elif d['status'] == 'finished':
            asyncio.create_task(self.progress.finish(True, ""))

async def download_with_ytdlp_direct(url: str, status_message: Message) -> Tuple[Optional[str], Optional[str], int, Optional[str]]:
    """Download video directly using yt-dlp (best method - no 403!)"""
    try:
        filename = f"video_{int(time.time())}.%(ext)s"
        filepath_template = os.path.join(OUTPUT_FOLDER, filename)
        
        # Get video info first to show duration and size
        await status_message.edit("📊 Getting video information...")
        
        ydl_opts_info = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        loop = asyncio.get_event_loop()
        def get_info():
            with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
                return ydl.extract_info(url, download=False)
        
        info = await loop.run_in_executor(None, get_info)
        
        if info is None:
            return None, "Could not get video info", 0, None
        
        # Extract duration
        duration = info.get('duration', 0)
        duration_str = ""
        if duration:
            if duration < 60:
                duration_str = f"{int(duration)} sec"
            elif duration < 3600:
                mins = int(duration // 60)
                secs = int(duration % 60)
                duration_str = f"{mins}:{secs:02d} min"
            else:
                hours = int(duration // 3600)
                mins = int((duration % 3600) // 60)
                duration_str = f"{hours}:{mins:02d} hr"
        
        # Get estimated file size
        file_size = 0
        formats = info.get('formats', [])
        for f in formats:
            if f.get('filesize'):
                file_size = max(file_size, f.get('filesize', 0))
        
        if file_size > 0 and file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            return None, f"Video too large (>{MAX_FILE_SIZE_MB}MB)", 0, duration_str
        
        await status_message.edit(f"🎬 Video: {info.get('title', 'Unknown')[:50]}\n⏱️ Duration: {duration_str or 'Unknown'}\n⏬ Starting download...")
        
        # Download with progress hook
        ydl_opts = {
            'outtmpl': filepath_template,
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [YTDLPProgressHook(status_message, file_size).hook],
            'format': 'best[ext=mp4]/best',  # Prefer mp4
            'merge_output_format': 'mp4',
            'retries': 10,
            'fragment_retries': 10,
        }
        
        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.download([url])
        
        result = await loop.run_in_executor(None, download)
        
        if result != 0:
            return None, "Download failed", 0, duration_str
        
        # Find the downloaded file
        files = glob.glob(os.path.join(OUTPUT_FOLDER, "video_*"))
        if not files:
            return None, "Downloaded file not found", 0, duration_str
        
        filepath = max(files, key=os.path.getctime)
        actual_size = os.path.getsize(filepath)
        
        await status_message.edit(f"✅ Download complete! Size: {human_readable_size(actual_size)}")
        return filepath, None, actual_size, duration_str
        
    except Exception as e:
        logger.error(f"yt-dlp download error: {e}")
        return None, str(e), 0, None

# ========== DIRPY EXTRACTION (FALLBACK FOR NON-YOUTUBE) ==========
async def extract_download_link_from_dirpy(video_url: str, status_message: Message) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract download link from Dirpy for non-YouTube sites"""
    chromium_path = find_chromium()
    if not chromium_path:
        return None, "Chromium not found", None

    browser = None
    try:
        await status_message.edit("🌐 Launching browser for Dirpy...")
        browser = await launch(
            headless=True,
            executablePath=chromium_path,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
            defaultViewport={'width': 1280, 'height': 800}
        )

        page = await browser.newPage()
        encoded_url = quote(video_url, safe='')
        dirpy_url = f"https://dirpy.com/studio?url={encoded_url}"
        await page.goto(dirpy_url, {'waitUntil': 'networkidle0', 'timeout': 60000})
        await asyncio.sleep(12)
        
        # Extract duration
        duration_js = "() => { const v = document.querySelector('video'); return v && v.duration ? v.duration : null; }"
        video_duration = await page.evaluate(duration_js)
        duration_str = None
        if video_duration and video_duration > 0:
            if video_duration < 60:
                duration_str = f"{int(video_duration)} sec"
            elif video_duration < 3600:
                mins = int(video_duration // 60)
                secs = int(video_duration % 60)
                duration_str = f"{mins}:{secs:02d} min"
            else:
                hours = int(video_duration // 3600)
                mins = int((video_duration % 3600) // 60)
                duration_str = f"{hours}:{mins:02d} hr"
        
        # Extract video source
        js_get = """
            () => {
                const v = document.querySelector('video');
                if (v && v.src && v.src.startsWith('http')) return v.src;
                const s = document.querySelector('video source');
                if (s && s.src && s.src.startsWith('http')) return s.src;
                return null;
            }
        """
        download_url = await page.evaluate(js_get)
        
        if not download_url:
            return None, "Could not find video source", duration_str

        download_url = unescape(unquote(download_url))
        if download_url.startswith('/url?q='):
            download_url = download_url.replace('/url?q=', '').split('&')[0]

        return download_url, None, duration_str
        
    except Exception as e:
        logger.error(f"Dirpy error: {e}")
        return None, str(e), None
    finally:
        if browser:
            await browser.close()

# ========== HTML TO PDF ==========
async def html_to_pdf(url: str) -> Tuple[Optional[str], Optional[str], int]:
    if not PYPPETEER_AVAILABLE:
        return None, "pyppeteer not installed", 0
    chromium_path = find_chromium()
    if not chromium_path:
        return None, "Chromium not found", 0

    browser = None
    try:
        browser = await launch(
            headless=True,
            executablePath=chromium_path,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
            defaultViewport={'width': 1280, 'height': 800}
        )
        page = await browser.newPage()
        await page.goto(url, {'waitUntil': 'networkidle0', 'timeout': 60000})
        await page.evaluate('window.scrollTo(0, document.body.scrollHeight);')
        await asyncio.sleep(1)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filepath = os.path.join(OUTPUT_FOLDER, f"pdf_{timestamp}.pdf")
        await page.pdf({'path': filepath, 'format': 'A4', 'printBackground': True})
        size = os.path.getsize(filepath)
        return filepath, None, size
    except Exception as e:
        return None, str(e), 0
    finally:
        if browser:
            await browser.close()

# ========== CAPTURE MHTML ==========
async def capture_html(url: str) -> Tuple[Optional[str], Optional[str], int, bool]:
    if not PYPPETEER_AVAILABLE:
        return None, "pyppeteer not installed", 0, False
    chromium_path = find_chromium()
    if not chromium_path:
        return None, "Chromium not found", 0, False

    browser = None
    try:
        browser = await launch(
            headless=True,
            executablePath=chromium_path,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
        )
        page = await browser.newPage()
        await page.goto(url, {'waitUntil': 'networkidle0', 'timeout': 60000})
        await page.evaluate('window.scrollTo(0, document.body.scrollHeight);')
        await asyncio.sleep(2)
        
        client = await page.target.createCDPSession()
        mhtml = await client.send('Page.captureSnapshot', {'format': 'mhtml'})
        data = base64.b64decode(mhtml['data'])
        
        title = await page.title() or "webpage"
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title[:50])
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filepath = os.path.join(OUTPUT_FOLDER, f"snapshot_{safe_title}_{timestamp}.mhtml")
        
        with open(filepath, 'wb') as f:
            f.write(data)
        
        size = os.path.getsize(filepath)
        is_zip = False
        
        if size > 40 * 1024 * 1024:
            zip_path = os.path.join(OUTPUT_FOLDER, f"snapshot_{safe_title}_{timestamp}.zip")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(filepath, os.path.basename(filepath))
            os.remove(filepath)
            filepath, size, is_zip = zip_path, os.path.getsize(zip_path), True
        
        return filepath, None, size, is_zip
    except Exception as e:
        return None, str(e), 0, False
    finally:
        if browser:
            await browser.close()

# ========== PROCESSING FUNCTIONS ==========
processing_messages = set()

async def safe_edit(msg, text):
    try:
        await msg.edit(text, parse_mode='markdown')
    except telethon_errors.MessageNotModifiedError:
        pass

async def process_dirpy_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)

    status_msg = await event.reply("🔄 Processing...", parse_mode='markdown')
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # Check if it's a YouTube URL - use direct yt-dlp
        if 'youtube.com/watch' in url or 'youtu.be/' in url or 'youtube.com/shorts' in url:
            filepath, error, size, duration = await download_with_ytdlp_direct(url, status_msg)
            
            if error or not filepath:
                await safe_edit(status_msg, f"❌ {error}")
                return
            
            caption = f"🎬 **Video ready!**\n📦 {human_readable_size(size)}"
            if duration:
                caption += f"\n⏱️ Duration: {duration}"
            
            await event.client.send_file(
                event.chat_id, filepath, 
                caption=caption, 
                supports_streaming=True, 
                force_document=False
            )
            os.remove(filepath)
            await status_msg.delete()
            return
        
        # For non-YouTube sites, try Dirpy
        await safe_edit(status_msg, "🔄 Non-YouTube link detected, trying Dirpy...")
        download_url, error, duration = await extract_download_link_from_dirpy(url, status_msg)
        
        if error or not download_url:
            await safe_edit(status_msg, f"❌ {error}\n\n💡 This site may not be supported by Dirpy")
            return
        
        await safe_edit(status_msg, f"✅ Link found! Downloading...")
        
        # Try direct download with headers
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://dirpy.com/',
            }
            timeout = ClientTimeout(total=DOWNLOAD_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(download_url, headers=headers, allow_redirects=True) as response:
                    if response.status != 200:
                        await safe_edit(status_msg, f"❌ HTTP {response.status}")
                        return
                    
                    total_size = int(response.headers.get('content-length', 0))
                    filepath = os.path.join(OUTPUT_FOLDER, f"video_{int(time.time())}.mp4")
                    progress = ProgressHandler(status_msg, total_size, "Downloading video")
                    
                    downloaded = 0
                    async with aiofiles.open(filepath, 'wb') as f:
                        async for chunk in response.content.iter_chunked(8192):
                            if chunk:
                                await f.write(chunk)
                                downloaded += len(chunk)
                                await progress.update(downloaded)
                    
                    await progress.finish(True, "")
                    
                    caption = f"🎬 **Video ready!**\n📦 {human_readable_size(downloaded)}"
                    if duration:
                        caption += f"\n⏱️ Duration: {duration}"
                    
                    await event.client.send_file(
                        event.chat_id, filepath,
                        caption=caption,
                        supports_streaming=True,
                        force_document=False
                    )
                    os.remove(filepath)
                    await status_msg.delete()
                    
        except Exception as e:
            await safe_edit(status_msg, f"❌ Download failed: {str(e)[:100]}")
            
    except Exception as e:
        logger.error(f"Process error: {e}")
        await safe_edit(status_msg, f"❌ Error: {str(e)[:100]}")
    finally:
        processing_messages.discard(msg_id)

async def process_pdf_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    status_msg = await event.reply("🔄 Converting to PDF...", parse_mode='markdown')
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        filepath, error, size = await html_to_pdf(url)
        if error:
            await safe_edit(status_msg, f"❌ {error}")
            return
        await event.client.send_file(event.chat_id, filepath, caption="📄 PDF ready", force_document=True)
        os.remove(filepath)
        await status_msg.delete()
    except Exception as e:
        await safe_edit(status_msg, f"❌ {str(e)}")
    finally:
        processing_messages.discard(msg_id)

async def process_html_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    status_msg = await event.reply("🔄 Capturing webpage...", parse_mode='markdown')
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        filepath, error, size, is_zip = await capture_html(url)
        if error:
            await safe_edit(status_msg, f"❌ {error}")
            return
        await event.client.send_file(event.chat_id, filepath, caption="📄 Complete webpage snapshot")
        os.remove(filepath)
        await status_msg.delete()
    except Exception as e:
        await safe_edit(status_msg, f"❌ {str(e)}")
    finally:
        processing_messages.discard(msg_id)

# ========== HANDLERS ==========
@events.register(events.NewMessage(pattern='/start', incoming=True))
async def start_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        await event.reply("⛔ Unauthorized")
        return
    await event.reply(
        "📄 **Ultimate Bot**\n\n"
        "**Commands:**\n"
        "🎬 `/dirpy <youtube-url>` → Download video directly (NO 403!)\n"
        "🌐 `/html <url>` → Save webpage as MHTML\n"
        "📑 `/pdf <url>` → Convert to PDF\n"
        "📥 Send direct link → Download with progress bar",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/help', incoming=True))
async def help_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    await event.reply(
        "**Commands:**\n"
        "- `/dirpy <youtube-url>` : Download YouTube videos directly\n"
        "- `/html <url>` : Save complete webpage\n"
        "- `/pdf <url>` : Convert to PDF\n"
        "- Send any link : Direct download",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/status', incoming=True))
async def status_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    chromium = find_chromium()
    await event.reply(
        f"**Bot Status**\n\n"
        f"• Chromium: {'✅' if chromium else '❌'}\n"
        f"• Pyppeteer: {'✅' if PYPPETEER_AVAILABLE else '❌'}\n"
        f"• yt-dlp: ✅\n"
        f"• Max Size: {MAX_FILE_SIZE_MB}MB",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/dirpy', incoming=True))
async def dirpy_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        await event.reply("⛔ Unauthorized")
        return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usage: `/dirpy <url>`", parse_mode='markdown')
        return
    await process_dirpy_request(event, parts[1].strip())

@events.register(events.NewMessage(pattern='/html', incoming=True))
async def html_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        await event.reply("⛔ Unauthorized")
        return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usage: `/html <url>`", parse_mode='markdown')
        return
    await process_html_request(event, parts[1].strip())

@events.register(events.NewMessage(pattern='/pdf', incoming=True))
async def pdf_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        await event.reply("⛔ Unauthorized")
        return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usage: `/pdf <url>`", parse_mode='markdown')
        return
    await process_pdf_request(event, parts[1].strip())

@events.register(events.NewMessage(incoming=True))
async def direct_handler(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    if event.raw_text.startswith('/'):
        return
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', event.raw_text)
    if not urls:
        return
    url = urls[0]
    
    if 'youtube.com/watch' in url or 'youtu.be/' in url:
        await event.reply("🎬 Send YouTube links with `/dirpy` for best results.", parse_mode='markdown')
        return
    
    status_msg = await event.reply("⏬ Downloading...", parse_mode='markdown')
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        timeout = ClientTimeout(total=DOWNLOAD_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as response:
                if response.status != 200:
                    await safe_edit(status_msg, f"❌ HTTP {response.status}")
                    return
                total_size = int(response.headers.get('content-length', 0))
                filepath = os.path.join(OUTPUT_FOLDER, f"file_{int(time.time())}.mp4")
                progress = ProgressHandler(status_msg, total_size, "Downloading")
                downloaded = 0
                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in response.content.iter_chunked(8192):
                        if chunk:
                            await f.write(chunk)
                            downloaded += len(chunk)
                            await progress.update(downloaded)
                await progress.finish(True, "")
                await event.client.send_file(event.chat_id, filepath, supports_streaming=True, force_document=False)
                os.remove(filepath)
                await status_msg.delete()
    except Exception as e:
        await safe_edit(status_msg, f"❌ {str(e)[:100]}")

# ========== MAIN ==========
async def main():
    print("\n" + "="*50)
    print("📄 ULTIMATE BOT (yt-dlp DIRECT DOWNLOAD)")
    print("="*50)
    chromium = find_chromium()
    if chromium:
        print(f"✅ Chromium: {chromium}")
    print("🤖 Starting...\n")

    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    
    client.add_event_handler(start_cmd)
    client.add_event_handler(help_cmd)
    client.add_event_handler(status_cmd)
    client.add_event_handler(dirpy_cmd)
    client.add_event_handler(html_cmd)
    client.add_event_handler(pdf_cmd)
    client.add_event_handler(direct_handler)
    
    me = await client.get_me()
    print(f"✅ Bot: @{me.username}")
    print("🎉 Ready!\n")
    await client.run_until_disconnected()

if __name__ == '__main__':
    start_keep_alive()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Stopped")
