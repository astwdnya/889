#!/usr/bin/env python3
# Telegram Ultimate Bot - Full Version 2026
# Features: yt-dlp + Dirpy Fallback + PDF + MHTML + Progress Bar + Docker Ready

import asyncio
import os
import re
import sys
import logging
import time
import zipfile
import base64
from datetime import datetime
from urllib.parse import quote
from typing import Optional, Tuple, Dict, Any

from flask import Flask
from threading import Thread

import aiohttp
import aiofiles
from aiohttp import ClientTimeout, ClientError

import yt_dlp
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from telethon import TelegramClient, events, errors as telethon_errors
from telethon.tl.types import Message

# ====================== CONFIGURATION ======================
BOT_TOKEN = "7675664254:AAHL7QhPonc47z0QKRFnB5p_L15SRiLBddc"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

AUTHORIZED_USERS = {818185073, 6936101187, 7972834913}

MAX_FILE_SIZE_MB = 2000
DOWNLOAD_TIMEOUT = 300
DIRPY_TIMEOUT = 60

OUTPUT_FOLDER = "output_files"
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

HEALTH_PORT = int(os.environ.get('PORT', 10000))

# ====================== LOGGING ======================
logging.basicConfig(
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("UltimateBot")

# ====================== FLASK KEEP-ALIVE ======================
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "✅ Ultimate Telegram Bot is running!", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=HEALTH_PORT, debug=False, use_reloader=False)

def start_keep_alive():
    Thread(target=run_flask, daemon=True).start()
    logger.info(f"🌐 Keep-alive server started on port {HEALTH_PORT}")

# ====================== UTILITIES ======================
def human_readable_size(num_bytes: int) -> str:
    if num_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} PB"

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
            if now - self.last_update_time < 1.2 and current_bytes != self.total_size:
                return

            if self.total_size > 0:
                percent = (current_bytes / self.total_size) * 100
                speed = (current_bytes - self.last_bytes) / (now - self.last_update_time) if self.last_update_time > 0 else 0
                eta = (self.total_size - current_bytes) / speed if speed > 0 else 0

                bar_length = 18
                filled = int(bar_length * current_bytes // self.total_size)
                bar = '█' * filled + '░' * (bar_length - filled)

                text = (
                    f"**{self.operation}**\n"
                    f"`[{bar}]` **{percent:.1f}%**\n"
                    f"📦 {human_readable_size(current_bytes)} / {human_readable_size(self.total_size)}\n"
                    f"🚀 {human_readable_size(int(speed))}/s • ⏱️ {int(eta//60)}m {int(eta%60)}s"
                )
            else:
                text = f"**{self.operation}...**\n📥 {human_readable_size(current_bytes)} downloaded"

            try:
                await self.status_message.edit(text, parse_mode='markdown')
            except telethon_errors.MessageNotModifiedError:
                pass
            except Exception as e:
                logger.debug(f"Edit error: {e}")

            self.last_update_time = now
            self.last_bytes = current_bytes

    async def finish(self, success: bool, final_message: str = ""):
        try:
            if success:
                await self.status_message.delete()
            else:
                await self.status_message.edit(f"❌ {final_message}", parse_mode='markdown')
        except Exception:
            pass

# ====================== DOWNLOAD FUNCTIONS ======================
async def download_with_yt_dlp(url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str], Optional[Dict]]:
    ydl_opts: Dict[str, Any] = {
        'outtmpl': f'{OUTPUT_FOLDER}/%(title)s.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'merge_output_format': 'mp4',
        'progress_hooks': [],
    }

    try:
        await status_msg.edit("🔍 Extracting information with yt-dlp...")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            filepath = ydl.prepare_filename(info)

        progress = ProgressHandler(status_msg, info.get('filesize_approx') or info.get('filesize') or 0, "Downloading Video")

        def progress_hook(d):
            if d['status'] == 'downloading':
                downloaded = d.get('downloaded_bytes', 0)
                asyncio.create_task(progress.update(downloaded))

        ydl_opts['progress_hooks'] = [progress_hook]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        await progress.finish(True, "")
        return filepath, None, info

    except Exception as e:
        logger.error(f"yt-dlp error: {e}", exc_info=True)
        return None, f"yt-dlp failed: {str(e)[:120]}", None


async def download_direct_with_progress(url: str, status_msg: Message, referer: Optional[str] = None) -> Tuple[Optional[str], Optional[str], int]:
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        }
        if referer:
            headers['Referer'] = referer

        timeout = ClientTimeout(total=DOWNLOAD_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as response:
                if response.status != 200:
                    return None, f"HTTP Error {response.status}", 0

                # هوشمندانه نام فایل گرفتن
                cd = response.headers.get('Content-Disposition', '')
                filename = f"video_{int(time.time())}.mp4"
                if 'filename=' in cd:
                    match = re.search(r'filename="?([^";]+)', cd)
                    if match:
                        filename = match.group(1).strip()

                filepath = os.path.join(OUTPUT_FOLDER, safe_filename(filename))
                total_size = int(response.headers.get('content-length', 0))

                if total_size > MAX_FILE_SIZE_MB * 1024 * 1024:
                    return None, f"File too large (max {MAX_FILE_SIZE_MB}MB)", 0

                progress = ProgressHandler(status_msg, total_size, "Downloading")
                downloaded = 0

                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in response.content.iter_chunked(8192):
                        if chunk:
                            await f.write(chunk)
                            downloaded += len(chunk)
                            await progress.update(downloaded)

                await progress.finish(True, "")
                return filepath, None, downloaded

    except Exception as e:
        logger.error(f"Direct download error: {e}")
        return None, str(e), 0

# ====================== PLAYWRIGHT HELPERS ======================
async def extract_with_dirpy_playwright(video_url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str]]:
    async with async_playwright() as p:
        browser = None
        captured_url = None
        try:
            await status_msg.edit("🌐 Launching browser for network interception...")
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
            )
            page = await browser.new_page()
            await page.set_viewport_size({"width": 1280, "height": 800})

            async def handle_request(request):
                nonlocal captured_url
                if 'googlevideo.com/videoplayback' in request.url and 'itag=' in request.url:
                    if not captured_url:
                        captured_url = request.url
                        logger.info(f"✅ Captured video URL: {request.url[:120]}...")

            await page.route("**/*", handle_request)

            dirpy_url = f"https://dirpy.com/studio?url={quote(video_url)}"
            await page.goto(dirpy_url, wait_until="domcontentloaded", timeout=45000)

            await page.wait_for_timeout(12000)  # صبر برای پخش ویدیو

            if not captured_url:
                await page.evaluate('() => document.querySelector("video")?.play()')
                await page.wait_for_timeout(8000)

            if captured_url:
                return captured_url, None
            return None, "Could not capture video stream URL"

        except Exception as e:
            logger.error(f"Dirpy Playwright error: {e}")
            return None, str(e)
        finally:
            if browser:
                await browser.close()

# ====================== PDF & MHTML ======================
async def html_to_pdf(url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str], int]:
    async with async_playwright() as p:
        browser = None
        try:
            await status_msg.edit("📄 Converting webpage to PDF...")
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

            filepath = os.path.join(OUTPUT_FOLDER, f"pdf_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
            await page.pdf({'path': filepath, 'format': 'A4', 'print_background': True})
            size = os.path.getsize(filepath)
            return filepath, None, size
        except Exception as e:
            return None, str(e), 0
        finally:
            if browser:
                await browser.close()

async def capture_mhtml(url: str, status_msg: Message) -> Tuple[Optional[str], Optional[str], int]:
    async with async_playwright() as p:
        browser = None
        try:
            await status_msg.edit("🌐 Capturing full webpage as MHTML...")
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

            client = await page.context.new_cdp_session(page)
            mhtml = await client.send('Page.captureSnapshot', {'format': 'mhtml'})
            data = mhtml['data'].encode()

            title = safe_filename(await page.title() or "webpage")
            filepath = os.path.join(OUTPUT_FOLDER, f"snapshot_{title}_{int(time.time())}.mhtml")

            async with aiofiles.open(filepath, 'wb') as f:
                await f.write(data)

            return filepath, None, len(data)
        except Exception as e:
            return None, str(e), 0
        finally:
            if browser:
                await browser.close()

# ====================== PROCESSING CORE ======================
processing_messages = set()

async def safe_edit(msg: Message, text: str):
    try:
        await msg.edit(text, parse_mode='markdown')
    except Exception:
        pass

async def process_dirpy_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)

    status_msg = await event.reply("🔄 Initializing advanced video downloader...", parse_mode='markdown')

    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        # اول yt-dlp
        filepath, error, info = await download_with_yt_dlp(url, status_msg)

        if error or not filepath:
            await safe_edit(status_msg, "⚠️ yt-dlp failed. Trying Dirpy fallback...")
            direct_url, dirpy_err = await extract_with_dirpy_playwright(url, status_msg)
            if dirpy_err or not direct_url:
                await safe_edit(status_msg, f"❌ Both methods failed: {dirpy_err}")
                return

            referer = f"https://dirpy.com/studio?url={quote(url)}"
            filepath, dl_error, file_size = await download_direct_with_progress(direct_url, status_msg, referer)
            if dl_error:
                await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
                return
            duration = None
        else:
            file_size = os.path.getsize(filepath)
            duration = format_duration(info.get('duration'))

        # آپلود نهایی
        await safe_edit(status_msg, "📤 Uploading to Telegram...")
        caption = f"🎬 **Video Downloaded Successfully**\n📦 Size: {human_readable_size(file_size)}"
        if duration:
            caption += f"\n⏱️ Duration: {duration}"
        caption += f"\n🔗 [Original Link]({url})"

        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=caption,
            supports_streaming=True,
            parse_mode='markdown'
        )

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Dirpy process failed: {e}", exc_info=True)
        await safe_edit(status_msg, f"❌ Unexpected error: {str(e)[:100]}")
    finally:
        processing_messages.discard(msg_id)
        # Cleanup
        try:
            if 'filepath' in locals() and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass

async def process_pdf_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages: return
    processing_messages.add(msg_id)
    status = await event.reply("📑 Converting to PDF...")

    try:
        if not url.startswith(('http', 'https')):
            url = 'https://' + url
        filepath, error, size = await html_to_pdf(url, status)
        if error:
            await safe_edit(status, f"❌ {error}")
            return
        await event.client.send_file(event.chat_id, filepath, caption=f"📄 PDF • {human_readable_size(size)}", force_document=True)
        await status.delete()
    finally:
        processing_messages.discard(msg_id)
        try: os.remove(filepath)
        except: pass

async def process_html_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages: return
    processing_messages.add(msg_id)
    status = await event.reply("📦 Capturing complete webpage...")

    try:
        if not url.startswith(('http', 'https')):
            url = 'https://' + url
        filepath, error, size = await capture_mhtml(url, status)
        if error:
            await safe_edit(status, f"❌ {error}")
            return
        await event.client.send_file(event.chat_id, filepath, caption="🌐 Full Webpage Snapshot (MHTML)")
        await status.delete()
    finally:
        processing_messages.discard(msg_id)
        try: os.remove(filepath)
        except: pass

# ====================== TELEGRAM HANDLERS ======================
@events.register(events.NewMessage(pattern='/start', incoming=True))
async def start_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized user.")
    await event.reply(
        "🚀 **Ultimate Telegram Bot**\n\n"
        "Available Commands:\n"
        "• `/dirpy <url>` → Download video (YouTube, etc.)\n"
        "• `/pdf <url>` → Convert webpage to PDF\n"
        "• `/html <url>` → Save full page as MHTML\n"
        "• Send any direct link → Auto download with progress\n\n"
        "Powered by yt-dlp + Playwright",
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
    if event.sender_id not in AUTHORIZED_USERS: return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2: return await event.reply("❌ Usage: `/pdf <url>`")
    await process_pdf_request(event, parts[1].strip())

@events.register(events.NewMessage(pattern='/html', incoming=True))
async def html_command(event):
    if event.sender_id not in AUTHORIZED_USERS: return
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2: return await event.reply("❌ Usage: `/html <url>`")
    await process_html_request(event, parts[1].strip())

@events.register(events.NewMessage(incoming=True))
async def generic_url_handler(event):
    if event.sender_id not in AUTHORIZED_USERS or event.raw_text.startswith('/'):
        return
    urls = re.findall(r'https?://[^\s<>"\']+', event.raw_text)
    if not urls:
        return
    status_msg = await event.reply("⏬ Downloading direct link...")
    filepath, error, size = await download_direct_with_progress(urls[0], status_msg)
    if error or not filepath:
        await safe_edit(status_msg, f"❌ {error or 'Download failed'}")
        return
    await event.client.send_file(event.chat_id, filepath, supports_streaming=True)
    await status_msg.delete()
    try: os.remove(filepath)
    except: pass

# ====================== MAIN ======================
async def main():
    print("\n" + "="*70)
    print("🚀 ULTIMATE TELEGRAM BOT - FULL VERSION")
    print("   yt-dlp + Playwright + Progress Bar + Docker")
    print("="*70)

    if not BOT_TOKEN or API_ID == 0 or not API_HASH:
        logger.critical("❌ BOT_TOKEN, API_ID or API_HASH is missing!")
        sys.exit(1)

    start_keep_alive()

    client = TelegramClient('ultimate_bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    client.add_event_handler(start_cmd)
    client.add_event_handler(dirpy_command)
    client.add_event_handler(pdf_command)
    client.add_event_handler(html_command)
    client.add_event_handler(generic_url_handler)

    me = await client.get_me()
    logger.info(f"✅ Bot successfully started as @{me.username}")
    print(f"✅ Bot is online → @{me.username}\n")

    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
