#!/usr/bin/env python3
# Telegram Ultimate Bot - Fixed HTTP 403 and added video duration detection

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

# --- Web scraping & downloads ---
import aiohttp
from aiohttp import ClientTimeout, ClientError
import aiofiles
from bs4 import BeautifulSoup

# --- PDF & HTML capture ---
try:
    from pyppeteer import launch
    PYPPETEER_AVAILABLE = True
except ImportError:
    PYPPETEER_AVAILABLE = False
    print("⚠️ pyppeteer not installed, PDF/HTML features disabled.")

# ========== CONFIGURATION ==========
BOT_TOKEN = "7675664254:AAHL7QhPonc47z0QKRFnB5p_L15SRiLBddc"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
AUTHORIZED_USERS = {818185073, 6936101187, 7972834913}

MAX_FILE_SIZE_MB = 2000
DIRPY_DOWNLOAD_TIMEOUT = 180  # Increased timeout
DIRPY_PAGE_TIMEOUT = 90       # Increased page timeout

# Folders
OUTPUT_FOLDER = "output_files"
CHROMIUM_DOWNLOADS = os.path.join(OUTPUT_FOLDER, "chromium_downloads_do_not_delete")
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(CHROMIUM_DOWNLOADS, exist_ok=True)

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
def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS

def find_chromium() -> Optional[str]:
    possible_paths = [
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"
    ]
    for path in possible_paths:
        if os.path.exists(path):
            logger.info(f"✅ Chromium found: {path}")
            return path
    logger.error("❌ Chromium not found!")
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

def format_duration(seconds: float) -> str:
    """Convert seconds to MM:SS or HH:MM:SS format"""
    if seconds < 60:
        return f"{int(seconds)} seconds"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d} minutes"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}:{minutes:02d} hours"

# ========== PROGRESS BAR HANDLER ==========
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

# ========== FILE DOWNLOADER WITH PROGRESS AND HEADERS ==========
async def download_file_with_progress(url: str, status_message: Message, referer_url: str = None) -> Tuple[Optional[str], Optional[str], int]:
    """Download file with proper headers to avoid 403 errors"""
    timeout = ClientTimeout(total=DIRPY_DOWNLOAD_TIMEOUT)
    
    # Critical: Add headers to avoid 403 Forbidden
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'video/webm,video/mp4,video/*;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Range': 'bytes=0-',
        'Connection': 'keep-alive',
    }
    
    if referer_url:
        headers['Referer'] = referer_url
    
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as response:
                if response.status == 403:
                    return None, "HTTP 403 Forbidden - Try again with different headers", 0
                if response.status != 200:
                    return None, f"HTTP {response.status}", 0

                total_size = int(response.headers.get('content-length', 0))
                if total_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                    return None, f"File too large (>{MAX_FILE_SIZE_MB}MB)", 0

                # Extract filename from URL or content-disposition
                filename = None
                content_disposition = response.headers.get('content-disposition', '')
                if 'filename=' in content_disposition:
                    filename = content_disposition.split('filename=')[-1].strip('"\'')
                
                if not filename:
                    # Extract from URL
                    url_filename = url.split('/')[-1].split('?')[0]
                    if url_filename and '.' in url_filename:
                        filename = url_filename
                    else:
                        filename = f"video_{int(time.time())}.mp4"
                
                if not filename.lower().endswith(('.mp4', '.mkv', '.webm', '.mov')):
                    filename += '.mp4'

                filepath = os.path.join(OUTPUT_FOLDER, filename)
                progress = ProgressHandler(status_message, total_size, "Downloading video")

                downloaded = 0
                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in response.content.iter_chunked(8192):
                        if chunk:
                            await f.write(chunk)
                            downloaded += len(chunk)
                            await progress.update(downloaded)

                await progress.finish(True, "")
                return filepath, None, downloaded

    except asyncio.TimeoutError:
        return None, "Download timeout", 0
    except ClientError as e:
        return None, f"Network error: {str(e)[:50]}", 0
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None, f"Unexpected error: {str(e)[:50]}", 0

# ========== IMPROVED DIRPY LOGIC WITH VIDEO DURATION ==========
async def extract_download_link_from_dirpy(video_url: str, status_message: Message) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Opens Dirpy page, waits for video to load, extracts source URL and duration.
    Returns: (download_url, error_message, duration_str)
    """
    chromium_path = find_chromium()
    if not chromium_path:
        return None, "Chromium not found", None

    browser = None
    try:
        await status_message.edit("🌐 Launching browser...")
        browser = await launch(
            headless=True,
            executablePath=chromium_path,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                '--disable-gpu', '--disable-software-rasterizer'
            ],
            defaultViewport={'width': 1280, 'height': 800}
        )

        page = await browser.newPage()
        encoded_url = quote(video_url, safe='')
        dirpy_url = f"https://dirpy.com/studio?url={encoded_url}"
        await status_message.edit(f"📄 Opening Dirpy Studio...")
        await page.goto(dirpy_url, {'waitUntil': 'networkidle0', 'timeout': DIRPY_PAGE_TIMEOUT * 1000})

        # Wait for video to load (at least 10-15 seconds)
        await status_message.edit("⏳ Waiting for video to load (this may take 10-15 seconds)...")
        
        # Wait for video element
        try:
            await page.wait_for_selector('video', timeout=30000)
        except Exception:
            pass
        
        # Wait additional time for video duration metadata to load
        await asyncio.sleep(12)  # Ensure video duration is loaded
        
        # Extract video duration
        duration_js = """
            () => {
                const video = document.querySelector('video');
                if (video && video.duration && !isNaN(video.duration)) {
                    return video.duration;
                }
                return null;
            }
        """
        video_duration = await page.evaluate(duration_js)
        
        duration_str = None
        if video_duration and video_duration > 0:
            duration_str = format_duration(video_duration)
            await status_message.edit(f"⏳ Video loaded! Duration: {duration_str}\n🔗 Extracting download link...")
        else:
            await status_message.edit(f"⏳ Video loaded! Extracting download link...")
        
        # Extract the video source URL
        js_get_source = """
            () => {
                // Method 1: Check video element's src
                const video = document.querySelector('video');
                if (video && video.src && video.src.startsWith('http')) {
                    return video.src;
                }
                
                // Method 2: Check source tag inside video
                const source = document.querySelector('video source');
                if (source && source.src && source.src.startsWith('http')) {
                    return source.src;
                }
                
                // Method 3: Find google video link
                const links = Array.from(document.querySelectorAll('a'));
                const videoLink = links.find(link => 
                    link.href && link.href.includes('googlevideo.com')
                );
                if (videoLink) return videoLink.href;
                
                return null;
            }
        """
        
        download_url = await page.evaluate(js_get_source)
        
        if not download_url:
            # Fallback: parse HTML
            page_html = await page.content()
            soup = BeautifulSoup(page_html, 'html.parser')
            source_tag = soup.find('source')
            if source_tag and source_tag.get('src'):
                download_url = source_tag.get('src')
            
            if not download_url:
                video_tag = soup.find('video')
                if video_tag and video_tag.get('src'):
                    download_url = video_tag.get('src')

        if not download_url:
            return None, "Could not find video source link", duration_str

        # Clean URL
        download_url = unescape(download_url)
        download_url = unquote(download_url)
        
        if download_url.startswith('/url?q='):
            download_url = download_url.replace('/url?q=', '').split('&')[0]
        
        if not download_url.startswith('http'):
            return None, "Invalid URL format extracted", duration_str

        return download_url, None, duration_str

    except Exception as e:
        logger.error(f"Dirpy extraction error: {e}")
        return None, f"Automation failed: {str(e)[:100]}", None
    finally:
        if browser:
            await browser.close()

# ========== PDF & HTML FUNCTIONS ==========
async def capture_html(url: str, status_msg=None) -> Tuple[Optional[str], Optional[str], int, bool]:
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
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
            defaultViewport={'width': 1280, 'height': 800}
        )
        page = await browser.newPage()
        await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        await page.goto(url, {'waitUntil': 'networkidle0', 'timeout': 60000})

        await page.evaluate('window.scrollTo(0, document.body.scrollHeight);')
        await asyncio.sleep(2)
        await page.evaluate('window.scrollTo(0, 0);')

        client = await page.target.createCDPSession()
        mhtml_data = await client.send('Page.captureSnapshot', {'format': 'mhtml'})
        mhtml_bytes = base64.b64decode(mhtml_data['data'])

        title = await page.title() or "webpage"
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title[:50])
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"snapshot_{safe_title}_{timestamp}.mhtml"
        filepath = os.path.join(OUTPUT_FOLDER, filename)
        with open(filepath, 'wb') as f:
            f.write(mhtml_bytes)

        file_size = os.path.getsize(filepath)
        is_zip = False
        if file_size > 40 * 1024 * 1024:
            zip_filename = f"snapshot_{safe_title}_{timestamp}.zip"
            zip_filepath = os.path.join(OUTPUT_FOLDER, zip_filename)
            with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(filepath, filename)
            os.remove(filepath)
            filepath, file_size, is_zip = zip_filepath, os.path.getsize(zip_filepath), True

        return filepath, None, file_size, is_zip

    except Exception as e:
        logger.error(f"HTML capture error: {e}")
        return None, str(e), 0, False
    finally:
        if browser:
            await browser.close()

async def html_to_pdf(url: str, status_msg=None) -> Tuple[Optional[str], Optional[str], int]:
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
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
            defaultViewport={'width': 1280, 'height': 800}
        )
        page = await browser.newPage()
        await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        await page.goto(url, {'waitUntil': 'networkidle0', 'timeout': 60000})

        await page.evaluate('window.scrollTo(0, document.body.scrollHeight);')
        await asyncio.sleep(1)
        await page.evaluate('window.scrollTo(0, 0);')

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"pdf_{timestamp}.pdf"
        filepath = os.path.join(OUTPUT_FOLDER, filename)
        await page.pdf({'path': filepath, 'format': 'A4', 'printBackground': True})
        file_size = os.path.getsize(filepath)
        return filepath, None, file_size
    except Exception as e:
        return None, str(e), 0
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
    except Exception as e:
        logger.warning(f"Edit failed: {e}")

async def process_dirpy_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)

    status_msg = await event.reply("🔄 Initializing Dirpy downloader...", parse_mode='markdown')
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        download_url, error, duration = await extract_download_link_from_dirpy(url, status_msg)
        if error or not download_url:
            await safe_edit(status_msg, f"❌ {error}\n\n💡 Make sure the website is supported by Dirpy")
            return

        # Show duration in status if available
        duration_text = f"\n🎬 Duration: {duration}" if duration else ""
        await safe_edit(status_msg, f"✅ Link found!{duration_text}\n⏬ Starting download...")
        
        # Pass the dirpy URL as referer to avoid 403
        dirpy_referer = f"https://dirpy.com/studio?url={quote(url, safe='')}"
        filepath, dl_error, file_size = await download_file_with_progress(download_url, status_msg, dirpy_referer)
        
        if dl_error or not filepath:
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
            return

        await safe_edit(status_msg, "📤 Uploading to Telegram...")
        
        caption = f"🎬 **Video ready!**\n📦 {human_readable_size(file_size)}"
        if duration:
            caption += f"\n⏱️ Duration: {duration}"
        caption += f"\n🌐 [Source]({url[:50]})"
        
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=caption,
            supports_streaming=True,
            force_document=False,
            parse_mode='markdown'
        )
        
        try:
            os.remove(filepath)
        except:
            pass
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"Dirpy process error: {e}")
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

    status_msg = await event.reply("🔄 Capturing full webpage (MHTML)...", parse_mode='markdown')
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        filepath, error, size, is_zip = await capture_html(url)
        if error:
            await safe_edit(status_msg, f"❌ {error}")
            return
        caption = "📄 **Complete webpage snapshot**\n✅ All images, GIFs and links preserved"
        await event.client.send_file(event.chat_id, filepath, caption=caption)
        os.remove(filepath)
        await status_msg.delete()
    except Exception as e:
        await safe_edit(status_msg, f"❌ {str(e)}")
    finally:
        processing_messages.discard(msg_id)

# ========== TELEGRAM HANDLERS ==========
@events.register(events.NewMessage(pattern='/start', incoming=True))
async def start_cmd(event):
    if not is_authorized(event.sender_id):
        await event.reply("⛔ Unauthorized")
        return
    await event.reply(
        "📄 **Ultimate Bot**\n\n"
        "**Commands:**\n"
        "🌐 `/html <url>` → Save full page as MHTML (all images & GIFs, clickable links)\n"
        "📑 `/pdf <url>` → Print page to PDF\n"
        "🎬 `/dirpy <any-url>` → Download video (YouTube, FB, Twitter, etc.) via Dirpy\n"
        "📥 Send any direct link → Download with progress bar\n\n"
        "All downloads show **progress bars** and videos are sent as **native Telegram videos**.",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/help', incoming=True))
async def help_cmd(event):
    if not is_authorized(event.sender_id):
        return
    await event.reply(
        "**Available commands:**\n"
        "- `/html <url>` : captures the whole webpage as MHTML (everything included)\n"
        "- `/pdf <url>` : converts page to PDF\n"
        "- `/dirpy <url>` : uses Dirpy to get video from 1000+ sites\n"
        "- Send direct .mp4/.mkv link: downloads with progress bar and sends as video",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/status', incoming=True))
async def status_cmd(event):
    if not is_authorized(event.sender_id):
        return
    chromium = find_chromium()
    await event.reply(
        f"**Bot Status**\n\n"
        f"• Chromium: {'✅ Found' if chromium else '❌ Not found'}\n"
        f"• Pyppeteer: {'✅' if PYPPETEER_AVAILABLE else '❌'}\n"
        f"• Max File Size: {MAX_FILE_SIZE_MB}MB",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/dirpy', incoming=True))
async def dirpy_command(event):
    if not is_authorized(event.sender_id):
        await event.reply("⛔ Unauthorized")
        return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usage: `/dirpy https://example.com/video`", parse_mode='markdown')
        return
    await process_dirpy_request(event, parts[1].strip())

@events.register(events.NewMessage(pattern='/html', incoming=True))
async def html_command(event):
    if not is_authorized(event.sender_id):
        await event.reply("⛔ Unauthorized")
        return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usage: `/html <url>`", parse_mode='markdown')
        return
    await process_html_request(event, parts[1].strip())

@events.register(events.NewMessage(pattern='/pdf', incoming=True))
async def pdf_command(event):
    if not is_authorized(event.sender_id):
        await event.reply("⛔ Unauthorized")
        return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usage: `/pdf <url>`", parse_mode='markdown')
        return
    await process_pdf_request(event, parts[1].strip())

@events.register(events.NewMessage(incoming=True))
async def generic_url_handler(event):
    if not is_authorized(event.sender_id):
        return
    if event.raw_text.startswith('/'):
        return
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', event.raw_text)
    if not urls:
        return

    url = urls[0]
    status_msg = await event.reply("⏬ Downloading file...", parse_mode='markdown')
    filepath, error, size = await download_file_with_progress(url, status_msg)
    if error or not filepath:
        await safe_edit(status_msg, f"❌ {error}")
        return
    await event.client.send_file(event.chat_id, filepath, supports_streaming=True, force_document=False)
    os.remove(filepath)
    await status_msg.delete()

# ========== MAIN ==========
async def main():
    print("\n" + "="*50)
    print("📄 ULTIMATE BOT (PDF/HTML/DIRPY/DIRECT)")
    print("="*50)
    chromium = find_chromium()
    if chromium:
        print(f"✅ Chromium found: {chromium}")
    else:
        print("❌ Chromium NOT found! PDF/HTML features will fail.")
    print("🤖 Starting bot...\n")

    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    client.add_event_handler(start_cmd)
    client.add_event_handler(help_cmd)
    client.add_event_handler(status_cmd)
    client.add_event_handler(dirpy_command)
    client.add_event_handler(html_command)
    client.add_event_handler(pdf_command)
    client.add_event_handler(generic_url_handler)

    me = await client.get_me()
    print(f"✅ Bot: @{me.username}")
    print("🎉 Ready! Commands: /dirpy, /html, /pdf, /status\n")
    await client.run_until_disconnected()

if __name__ == '__main__':
    start_keep_alive()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Stopped")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
