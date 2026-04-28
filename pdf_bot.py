#!/usr/bin/env python3
# Telegram PDF Bot - Convert any webpage to PDF with images & GIFs

import asyncio
import os
import re
import sys
import logging
import glob
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask
from threading import Thread
from telethon import TelegramClient, events
from telethon.errors import RPCError, MessageNotModifiedError

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

MAX_PDF_SIZE_MB = 50
PDF_TIMEOUT = 60
PDF_FOLDER = "pdf_output"
HEALTH_PORT = int(os.environ.get('PORT', 10000))

os.makedirs(PDF_FOLDER, exist_ok=True)

# ========== LOGGING ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== FLASK KEEP-ALIVE ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "✅ PDF Bot is running!", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=HEALTH_PORT, debug=False, use_reloader=False)

def start_keep_alive():
    thread = Thread(target=run_flask, daemon=True)
    thread.start()

# ========== FIND CHROMIUM ==========
def find_chromium():
    """Find Chromium executable"""
    possible_paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            logger.info(f"Found Chromium: {path}")
            return path
    
    # Check pyppeteer's download
    try:
        home = os.path.expanduser("~")
        pyppeteer_paths = glob.glob(f"{home}/.local/share/pyppeteer/local-chromium/*/chrome-linux/chrome")
        if pyppeteer_paths:
            logger.info(f"Found pyppeteer Chromium: {pyppeteer_paths[0]}")
            return pyppeteer_paths[0]
    except:
        pass
    
    logger.error("Chromium not found")
    return None

CHROMIUM_PATH = find_chromium()

# ========== PDF CONVERSION ==========
async def html_to_pdf(url: str) -> tuple:
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
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu'
            ],
            defaultViewport={'width': 1920, 'height': 1080}
        )
        
        page = await browser.newPage()
        await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        await page.goto(url, {'waitUntil': 'networkidle2', 'timeout': PDF_TIMEOUT * 1000})
        
        # Scroll to bottom
        await page.evaluate('''
            async function scroll() {
                let totalHeight = 0;
                const distance = 200;
                const scrollHeight = document.body.scrollHeight;
                while (totalHeight < scrollHeight) {
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    await new Promise(r => setTimeout(r, 100));
                }
                window.scrollTo(0, 0);
            }
            await scroll();
        ''')
        
        # Create filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"pdf_{timestamp}.pdf"
        filepath = os.path.join(PDF_FOLDER, filename)
        
        await page.pdf({
            'path': filepath,
            'format': 'A4',
            'printBackground': True,
            'margin': {'top': '20px', 'bottom': '20px', 'left': '20px', 'right': '20px'}
        })
        
        file_size = os.path.getsize(filepath)
        return filepath, None, file_size
        
    except Exception as e:
        logger.error(f"PDF error: {e}")
        return None, str(e)[:100], 0
    finally:
        if browser:
            await browser.close()

# ========== TELEGRAM HANDLERS ==========
processing_messages = set()

async def safe_edit(msg, text):
    try:
        await msg.edit(text, parse_mode='markdown')
    except MessageNotModifiedError:
        pass
    except Exception as e:
        logger.warning(f"Edit failed: {e}")

async def process_pdf_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    
    # FIXED: removed extra } in the f-string below
    status_msg = await event.reply(f"🔄 Converting `{url[:50]}...`", parse_mode='markdown')
    
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        filepath, error, size = await html_to_pdf(url)
        
        if error or not filepath:
            await safe_edit(status_msg, f"❌ {error}")
            return
        
        size_mb = size / (1024 * 1024)
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=f"📄 PDF Ready!\n📦 {size_mb:.2f} MB",
            force_document=True
        )
        
        os.remove(filepath)
        await status_msg.delete()
        
    except Exception as e:
        await safe_edit(status_msg, f"❌ Error: {str(e)[:100]}")
    finally:
        processing_messages.discard(msg_id)

@events.register(events.NewMessage(pattern='/start', incoming=True))
async def start_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        await event.reply("⛔ Unauthorized")
        return
    
    chromium_status = "✅ Ready" if find_chromium() else "❌ Not installed"
    await event.reply(
        f"📄 **PDF Bot**\n\n"
        f"Send me any URL to convert to PDF.\n\n"
        f"**Status:** {chromium_status}\n"
        f"**Commands:** /help, /status, /pdf",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/help', incoming=True))
async def help_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    await event.reply(
        "**Usage:**\n"
        "Send a URL or /pdf https://example.com\n\n"
        "The bot will convert the webpage to PDF.",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/status', incoming=True))
async def status_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    
    chromium = find_chromium()
    await event.reply(
        f"**Bot Status**\n\n"
        f"Chromium: {'✅ ' + chromium if chromium else '❌ Not found'}\n"
        f"Pyppeteer: {'✅' if PYPPETEER_AVAILABLE else '❌'}\n"
        f"PDF Folder: {PDF_FOLDER}",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/pdf', incoming=True))
async def pdf_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        await event.reply("⛔ Unauthorized")
        return
    
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Please provide URL: `/pdf https://example.com`", parse_mode='markdown')
        return
    
    await process_pdf_request(event, parts[1].strip())

@events.register(events.NewMessage(incoming=True))
async def url_handler(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    
    if event.raw_text.startswith('/'):
        return
    
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', event.raw_text)
    if not urls:
        return
    
    await process_pdf_request(event, urls[0])

# ========== MAIN ==========
async def main():
    print("\n" + "="*50)
    print("📄 TELEGRAM PDF BOT")
    print("="*50)
    
    chromium_path = find_chromium()
    if chromium_path:
        print(f"✅ Chromium: {chromium_path}")
    else:
        print("❌ Chromium NOT FOUND!")
    
    print(f"✅ Pyppeteer: {'Installed' if PYPPETEER_AVAILABLE else 'Not installed'}")
    print("🤖 Starting...\n")
    
    client = TelegramClient('pdf_bot_session', API_ID, API_HASH)
    
    await client.start(bot_token=BOT_TOKEN)
    
    client.add_event_handler(start_cmd)
    client.add_event_handler(help_cmd)
    client.add_event_handler(status_cmd)
    client.add_event_handler(pdf_cmd)
    client.add_event_handler(url_handler)
    
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
    except Exception as e:
        print(f"Fatal: {e}")
        sys.exit(1)
