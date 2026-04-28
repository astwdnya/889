#!/usr/bin/env python3
# Telegram Ultimate Bot - PDF, HTML (MHTML), Dirpy (Any Site), Direct Links
# All-in-one with progress bars & video playback

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
from urllib.parse import urlparse, quote
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

MAX_FILE_SIZE_MB = 2000            # 2GB Telegram limit
DIRPY_DOWNLOAD_TIMEOUT = 120       # seconds for file download
DIRPY_PAGE_TIMEOUT = 60            # seconds for page load and operation

# Folders
OUTPUT_FOLDER = "output_files"
CHROMIUM_DOWNLOADS = os.path.join(OUTPUT_FOLDER, "chromium_downloads_do_not_delete")
DIRPY_DOWNLOAD_PATH = CHROMIUM_DOWNLOADS
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

# ========== FILE DOWNLOADER WITH PROGRESS ==========
async def download_file_with_progress(url: str, status_message: Message, headers: dict = None) -> Tuple[Optional[str], Optional[str], int]:
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

# ========== CORE DIRPY LOGIC (UNIVERSAL - NO URL RESTRICTION) ==========
async def extract_download_link_from_dirpy(video_url: str, status_message: Message) -> Tuple[Optional[str], Optional[str]]:
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
        encoded_url = quote(video_url, safe='')
        dirpy_url = f"https://dirpy.com/studio?url={encoded_url}"
        await status_message.edit(f"📄 Opening Dirpy Studio for:\n{video_url[:60]}...")
        await page.goto(dirpy_url, {'waitUntil': 'networkidle0', 'timeout': DIRPY_PAGE_TIMEOUT * 1000})
        await asyncio.sleep(3)

        await status_message.edit("⏳ Waiting for video info (loading metadata)...")
        try:
            await page.wait_for_selector('video', timeout=45000)
        except Exception:
            pass
        await asyncio.sleep(4)

        await status_message.edit("🔘 Looking for download button...")
        download_url = None

        # try common selectors
        selectors_to_try = [
            '#downloadButton', 'button.download-btn', '.download-button',
            'a[download]', 'button[data-action="download"]'
        ]
        clicked = False
        for selector in selectors_to_try:
            btn = await page.querySelector(selector)
            if btn:
                await btn.click()
                clicked = True
                await status_message.edit("🔘 Download button clicked, waiting for modal...")
                await asyncio.sleep(5)
                break

        if not clicked:
            js_click = '''
                () => {
                    const buttons = Array.from(document.querySelectorAll('button, a'));
                    const downloadBtn = buttons.find(btn => btn.innerText.toLowerCase().includes('download'));
                    if (downloadBtn) { downloadBtn.click(); return true; }
                    return false;
                }
            '''
            clicked = await page.evaluate(js_click)
            if clicked:
                await asyncio.sleep(5)

        if not clicked:
            return None, "Could not find the download button on Dirpy. The site might not be supported."

        await status_message.edit("🔗 Extracting video download link...")
        await asyncio.sleep(3)

        # look for direct link
        direct_link = await page.querySelector('a[href*="googlevideo.com"], a[href*=".mp4"], a[href*=".mkv"]')
        if direct_link:
            download_url = await page.evaluate('(element) => element.href', direct_link)

        if not download_url:
            page_html = await page.content()
            soup = BeautifulSoup(page_html, 'html.parser')
            video_links = soup.find_all('a', href=re.compile(r'https?://[^\s]+\.(mp4|mkv|webm|googlevideo\.com)'))
            if video_links:
                download_url = video_links[0]['href']

        if not download_url:
            js_extract = '''
                () => {
                    const links = Array.from(document.querySelectorAll('a'));
                    const videoLink = links.find(link => 
                        link.href && (link.href.includes('googlevideo.com') || 
                                     link.href.match(/\\.(mp4|mkv|webm)(\\?|$)/i))
                    );
                    return videoLink ? videoLink.href : null;
                }
            '''
            download_url = await page.evaluate(js_extract)

        if not download_url:
            return None, "Could not find the direct download link. The site might not be supported by Dirpy."

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

# ========== IMPROVED HTML CAPTURE (MHTML) ==========
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

        # scroll to bottom to trigger lazy-loading
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

        # simple scroll
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

        download_url, error = await extract_download_link_from_dirpy(url, status_msg)
        if error or not download_url:
            await safe_edit(status_msg, f"❌ {error}\n\n💡 Tip: Make sure the website is supported by Dirpy (YouTube, Facebook, Twitter, Vimeo, Dailymotion, etc.)")
            return

        await safe_edit(status_msg, "✅ Link found, starting download...")
        filepath, dl_error, file_size = await download_file_with_progress(download_url, status_msg)
        if dl_error or not filepath:
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
            return

        await safe_edit(status_msg, "📤 Uploading to Telegram...")
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=f"🎬 **Video ready!**\n📦 {human_readable_size(file_size)}\n🌐 {url[:50]}",
            supports_streaming=True,
            force_document=False
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
    # If it's a known video site, suggest Dirpy, but still try direct download
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
