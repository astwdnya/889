#!/usr/bin/env python3
# Telegram PDF & HTML & Dirpy Bot - One File Everything

import asyncio
import os
import re
import sys
import logging
import glob
import zipfile
import time
import hashlib
from datetime import datetime
from urllib.parse import urlparse, quote
from html import unescape
from typing import Optional, Tuple

from flask import Flask
from threading import Thread
from telethon import TelegramClient, events, errors as telethon_errors
from telethon.tl.types import Message

# --- Web Scraping & Download ---
import aiohttp
from aiohttp import ClientTimeout, ClientError
import aiofiles
from bs4 import BeautifulSoup

# --- PDF & HTML Capture ---
try:
    from pyppeteer import launch
    PYPPETEER_AVAILABLE = True
except ImportError:
    PYPPETEER_AVAILABLE = False
    print("⚠️ pyppeteer not installed, PDF/HTML features disabled.")

# ========== 1️⃣  CONFIGURATION ==========
BOT_TOKEN = "7675664254:AAHL7QhPonc47z0QKRFnB5p_L15SRiLBddc"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
AUTHORIZED_USERS = {818185073, 6936101187, 7972834913}

MAX_FILE_SIZE_MB = 2000        # 2GB Telegram limit (easily enough for 1080p)
DIRPY_DOWNLOAD_TIMEOUT = 120    # seconds for file download
DIRPY_PAGE_TIMEOUT = 45          # seconds for page load and operation

# Folders
OUTPUT_FOLDER = "output_files"
CHROMIUM_DOWNLOADS = os.path.join(OUTPUT_FOLDER, "chromium_downloads_do_not_delete")
DIRPY_DOWNLOAD_PATH = CHROMIUM_DOWNLOADS   # important: point to a subfolder
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(CHROMIUM_DOWNLOADS, exist_ok=True)

# Flask keep-alive
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

def is_youtube_url(url: str) -> bool:
    """Check if URL is a YouTube video or short."""
    patterns = [
        r'(?:www\.)?youtube\.com/watch\?v=',
        r'(?:www\.)?youtu\.be/',
        r'(?:www\.)?youtube\.com/shorts/'
    ]
    return any(re.search(p, url) for p in patterns)

def find_chromium() -> Optional[str]:
    """Find Chromium executable path for PDF/HTML capture."""
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

# ========== 2️⃣  PROGRESS BAR HANDLER ==========
class ProgressHandler:
    """Handles sending interactive progress bars to Telegram."""
    def __init__(self, status_message: Message, total_size: int, operation: str = "Downloading"):
        self.status_message = status_message
        self.total_size = total_size
        self.operation = operation
        self.last_update_time = 0
        self.last_bytes = 0
        self._lock = asyncio.Lock()

    async def update(self, current_bytes: int, speed_delta: Optional[float] = None):
        """Update the progress message with a bar and details."""
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
        """Finalize the progress message."""
        if success:
            await self.status_message.delete()
        else:
            await self.status_message.edit(f"❌ {final_message}", parse_mode='markdown')

# ========== 3️⃣  FILE DOWNLOADER WITH PROGRESS ==========
async def download_file_with_progress(url: str, status_message: Message, headers: dict = None) -> Tuple[Optional[str], Optional[str], int]:
    """
    Download a file from a direct URL, showing a progress bar.

    Returns:
        Tuple[filepath, error_message, file_size_bytes]
    """
    timeout = ClientTimeout(total=DIRPY_DOWNLOAD_TIMEOUT)
    
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers or {}) as response:
                if response.status != 200:
                    return None, f"HTTP {response.status}", 0

                total_size = int(response.headers.get('content-length', 0))
                if total_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                    return None, f"File too large (>{MAX_FILE_SIZE_MB}MB)", 0

                content_disposition = response.headers.get('content-disposition', '')
                filename = None
                if 'filename=' in content_disposition:
                    filename = content_disposition.split('filename=')[-1].strip('"\'')
                
                if not filename:
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

# ========== 4️⃣  CORE DIRPY LOGIC (HUMAN-LIKE SIMULATION) ==========
async def extract_download_link_from_dirpy(youtube_url: str, status_message: Message) -> Tuple[Optional[str], Optional[str]]:
    """
    Automate Dirpy to fetch the direct download link.
    """
    chromium_path = find_chromium()
    if not chromium_path:
        return None, "Chromium not found"

    browser = None
    try:
        await status_message.edit("🌐 Launching browser... (Dirpy will open now)")
        browser = await launch(
            headless=True,
            executablePath=chromium_path,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                '--disable-gpu', '--disable-software-rasterizer',
                f'--download.default_directory={DIRPY_DOWNLOAD_PATH}'
            ],
            defaultViewport={'width': 1280, 'height': 800}
        )

        page = await browser.newPage()
        
        # ---- Step 1: Load Dirpy Studio page wide ----
        encoded_url = quote(youtube_url, safe='')
        dirpy_url = f"https://dirpy.com/studio?url={encoded_url}"
        await status_message.edit("📄 Opening Dirpy Studio...")
        await page.goto(dirpy_url, {'waitUntil': 'networkidle0', 'timeout': DIRPY_PAGE_TIMEOUT * 1000})
        await asyncio.sleep(3)

        # ---- Step 2: Wait for video info to load ----
        await status_message.edit("⏳ Waiting for video info (loading metadata)...")
        try:
            await page.wait_for_selector('video', timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(4)

        # ---- Step 3: Find and click the download button ----
        await status_message.edit("🔘 Looking for download button...")
        download_btn_initial = await page.querySelector('#downloadButton')
        if download_btn_initial:
            await download_btn_initial.click()
            await asyncio.sleep(3)

        # ---- Step 4: Extract final download URL from page or events ----
        download_url = None
        retries = 3
        for attempt in range(retries):
            content = await page.content()
            soup = BeautifulSoup(content, 'html.parser')
            direct_links = soup.find_all('a', href=re.compile(r'https?://rr\d+---sn-[^\\s]+\\.googlevideo\\.com/videoplayback'))
            if direct_links:
                download_url = direct_links[0]['href']
                break
            await asyncio.sleep(2)

        if not download_url:
            return None, "Could not find the direct download link on Dirpy."

        decoded_url = unescape(download_url)
        if decoded_url.startswith('/url?q='):
            decoded_url = decoded_url.replace('/url?q=', '').split('&')[0]

        return decoded_url, None

    except Exception as e:
        logger.error(f"Dirpy extraction error: {e}")
        return None, f"Automation failed: {str(e)[:100]}"
    finally:
        if browser:
            await browser.close()

# ========== 5️⃣  PDF & HTML FUNCTIONS (SAME AS BEFORE) ==========
async def html_to_pdf(url: str, status_msg=None) -> Tuple[Optional[str], Optional[str], int]:
    """Convert webpage to PDF (static copy)."""
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

async def capture_html(url: str, status_msg=None) -> Tuple[Optional[str], Optional[str], int, bool]:
    """Capture webpage as HTML with live links."""
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
        html_content = await page.content()
        title = await page.title() or "webpage"
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title[:50])
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"html_{safe_title}_{timestamp}.html"
        filepath = os.path.join(OUTPUT_FOLDER, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        file_size = os.path.getsize(filepath)
        is_zip = False
        if file_size > 40 * 1024 * 1024:
            zip_filename = f"html_{safe_title}_{timestamp}.zip"
            zip_filepath = os.path.join(OUTPUT_FOLDER, zip_filename)
            with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(filepath, filename)
            os.remove(filepath)
            filepath, file_size, is_zip = zip_filepath, os.path.getsize(zip_filepath), True
        return filepath, None, file_size, is_zip
    except Exception as e:
        return None, str(e), 0, False
    finally:
        if browser:
            await browser.close()

# ========== 6️⃣  PROCESSING REQUEST ==========
processing_messages = set()

async def safe_edit(msg, text):
    try:
        await msg.edit(text, parse_mode='markdown')
    except telethon_errors.MessageNotModifiedError:
        pass
    except Exception as e:
        logger.warning(f"Edit failed: {e}")

async def process_dirpy_request(event, url: str):
    """Main entry point for /dirpy command."""
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)

    status_msg = await event.reply("🔄 Initializing Dirpy downloader...", parse_mode='markdown')
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        if not is_youtube_url(url):
            await safe_edit(status_msg, "❌ This command currently only supports YouTube URLs.")
            return

        # --- Step 1: Extract direct link via Dirpy automation ---
        download_url, error = await extract_download_link_from_dirpy(url, status_msg)
        if error or not download_url:
            await safe_edit(status_msg, f"❌ {error}")
            return

        # --- Step 2: Download the file with progress bar ---
        await safe_edit(status_msg, "✅ Link found, starting download...")
        filepath, dl_error, file_size = await download_file_with_progress(download_url, status_msg)
        if dl_error or not filepath:
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
            return

        # --- Step 3: Send as video (not document) ---
        await safe_edit(status_msg, "📤 Uploading to Telegram...")
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=f"🎬 **Video ready!**\n📦 {human_readable_size(file_size)}",
            supports_streaming=True,
            force_document=False
        )

        # --- Cleanup ---
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
    # ... (Your existing /pdf logic) ...
    # Keep it as is from your previous version.
    # For brevity, I've summarized it. You can keep your full logic here.
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    status_msg = await event.reply("Converting to PDF...")
    try:
        if not url.startswith(('http://', 'https://')): url = 'https://' + url
        filepath, error, size = await html_to_pdf(url)
        if error: await safe_edit(status_msg, f"❌ {error}"); return
        await event.client.send_file(event.chat_id, filepath, caption="📄 PDF ready", force_document=True)
        os.remove(filepath)
        await status_msg.delete()
    except Exception as e: await safe_edit(status_msg, f"❌ {str(e)}")
    finally: processing_messages.discard(msg_id)

async def process_html_request(event, url: str):
    # ... (Your existing /html logic) ...
    # Keep it as is from your previous version.
    # For brevity, I've summarized it. You can keep your full logic here.
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    status_msg = await event.reply("Capturing HTML...")
    try:
        if not url.startswith(('http://', 'https://')): url = 'https://' + url
        filepath, error, size, is_zip = await capture_html(url)
        if error: await safe_edit(status_msg, f"❌ {error}"); return
        caption = "📄 HTML with live links"
        await event.client.send_file(event.chat_id, filepath, caption=caption)
        os.remove(filepath)
        await status_msg.delete()
    except Exception as e: await safe_edit(status_msg, f"❌ {str(e)}")
    finally: processing_messages.discard(msg_id)

# ========== 7️⃣  TELEGRAM HANDLERS ==========
@events.register(events.NewMessage(pattern='/start', incoming=True))
async def start_cmd(event):
    if not is_authorized(event.sender_id):
        await event.reply("⛔ Unauthorized")
        return
    await event.reply(
        "📄 **Media & Web Bot**\n\n"
        "**Commands:**\n"
        "🌐 `/html <url>` → Save page as HTML (live links, fully interactive)\n"
        "📑 `/pdf <url>` → Print page to PDF\n"
        "🎬 `/dirpy <YouTube URL>` → Download video via Dirpy (simulates a real browser)\n"
        "📥 Send any direct link → Download with progress bar directly (no middleman)",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/help', incoming=True))
async def help_cmd(event):
    if not is_authorized(event.sender_id): return
    await event.reply(
        "**Available commands:**\n"
        "- `/html <url>` : Captures the webpage as an HTML file (works like a real browser, links clickable)\n"
        "- `/pdf <url>` : Converts the page to a PDF file\n"
        "- `/dirpy <YouTube link>` : Uses Dirpy to extract and download video (automated, shown with progress bar)\n"
        "- Send any direct `.mp4` or `.mkv` link: Downloads the video and sends it (with progress bar)",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/status', incoming=True))
async def status_cmd(event):
    if not is_authorized(event.sender_id): return
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
        await event.reply("❌ Usage: `/dirpy https://www.youtube.com/watch?v=...`", parse_mode='markdown')
        return
    await process_dirpy_request(event, parts[1].strip())

@events.register(events.NewMessage(pattern='/html', incoming=True))
async def html_command(event):
    if not is_authorized(event.sender_id):
        await event.reply("⛔ Unauthorized")
        return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usa: `/html <url>`", parse_mode='markdown')
        return
    await process_html_request(event, parts[1].strip())

@events.register(events.NewMessage(pattern='/pdf', incoming=True))
async def pdf_commando(event):
    if not is_authorized(event.sender_id): return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usa: `/pdf <url>`", parse_mode='markdown')
        return
    await process_pdf_request(event, parts[1].strip())

@events.register(events.NewMessage(incoming=True))
async def generic_url_handler(event):
    """If user sends a raw URL, treat it as a direct download link."""
    if not is_authorized(event.sender_id): return
    if event.raw_text.startswith('/'): return
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', event.raw_text)
    if not urls: return

    url = urls[0]
    if is_youtube_url(url):
        await event.reply("🎬 YouTube link detected. Use `/dirpy` for better quality.", parse_mode='markdown')
        return

    # Assume it's a direct media link
    status_msg = await event.reply("⏬ Downloading file...", parse_mode='markdown')
    filepath, error, size = await download_file_with_progress(url, status_msg)
    if error or not filepath:
        await safe_edit(status_msg, f"❌ {error}")
        return
    await event.client.send_file(event.chat_id, filepath, supports_streaming=True, force_document=False)
    os.remove(filepath)
    await status_msg.delete()

# ========== 8️⃣  MAIN ==========
async def main():
    print("\n" + "="*50)
    print("📄 ULTIMATE BOT (PDF/HTML/DIRPY)")
    print("="*50)
    chromium = find_chromium()
    if chromium: print(f"✅ Chromium found: {chromium}")
    else: print("❌ Chromium NOT found!")
    print("🤖 Starting bot...\n")

    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    client.add_event_handler(start_cmd)
    client.add_event_handler(help_cmd)
    client.add_event_handler(status_cmd)
    client.add_event_handler(dirpy_command)
    client.add_event_handler(html_command)
    client.add_event_handler(pdf_commando)
    client.add_event_handler(generic_url_handler)

    me = await client.get_me()
    print(f"✅ Bot: @{me.username}")
    print("🎉 Ready! Commands: /dirpy, /html, /pdf\n")
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
