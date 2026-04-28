#!/usr/bin/env python3
# Telegram PDF Bot - Convert any webpage to PDF with images & GIFs
# Fixed: Duplicate message handling issue

import asyncio
import os
import re
import sys
import logging
import glob
from datetime import datetime
from urllib.parse import urlparse

# Flask for keep-alive
from flask import Flask
from threading import Thread

# Telegram
from telethon import TelegramClient, events
from telethon.errors import RPCError, MessageNotModifiedError

# PDF & Web requirements
try:
    from pyppeteer import launch
    PYPPETEER_AVAILABLE = True
except ImportError:
    PYPPETEER_AVAILABLE = False
    print("⚠️ pyppeteer not installed. Run: pip install pyppeteer")

# ========== CONFIGURATION ==========
BOT_TOKEN = os.getenv('BOT_TOKEN', "7675664254:AAHL7QhPonc47z0QKRFnB5p_L15SRiLBddc")
API_ID = int(os.getenv('API_ID', 2040))
API_HASH = os.getenv('API_HASH', "b18441a1ff607e10a989891a5462e627")

AUTHORIZED_USERS_STR = os.getenv('AUTHORIZED_USERS', "818185073,6936101187,7972834913")
AUTHORIZED_USERS = {int(x.strip()) for x in AUTHORIZED_USERS_STR.split(',')}

MAX_PDF_SIZE_MB = int(os.getenv('MAX_PDF_SIZE_MB', 50))
PDF_TIMEOUT = int(os.getenv('PDF_TIMEOUT', 60))
PDF_FOLDER = os.getenv('PDF_FOLDER', "pdf_output")
HEALTH_PORT = int(os.getenv('PORT', 10000))
CHROMIUM_PATH = os.getenv('CHROMIUM_PATH', None)

# Set to track processing messages (prevent duplicate)
processing_messages = set()

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
def health_check():
    return {"status": "ok", "time": datetime.now().isoformat()}, 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=HEALTH_PORT, debug=False, use_reloader=False)

def start_keep_alive():
    thread = Thread(target=run_flask, daemon=True)
    thread.start()
    logger.info(f"Keep-alive on port {HEALTH_PORT}")

# ========== HELPER FUNCTIONS ==========
def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS

def find_chromium():
    global CHROMIUM_PATH
    if CHROMIUM_PATH and os.path.exists(CHROMIUM_PATH):
        return CHROMIUM_PATH
    
    env_path = os.getenv('CHROMIUM_PATH')
    if env_path and os.path.exists(env_path):
        CHROMIUM_PATH = env_path
        return CHROMIUM_PATH
    
    common_paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
    ]
    
    for path in common_paths:
        if os.path.exists(path):
            CHROMIUM_PATH = path
            return CHROMIUM_PATH
    
    return None

def sanitize_filename(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.replace('.', '_').replace('-', '_')
    path = re.sub(r'[^\w\-_]', '_', parsed.path[:50]) if parsed.path else 'home'
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"pdf_{domain}_{path}_{timestamp}.pdf"
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    return filename[:200]

# ========== PDF CONVERSION ==========
async def html_to_pdf(url: str, status_callback=None) -> tuple:
    if not PYPPETEER_AVAILABLE:
        return None, "pyppeteer not installed", 0
    
    chromium_path = find_chromium()
    if not chromium_path:
        return None, "Chromium not found", 0
    
    browser = None
    page = None
    
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
        
        # Scroll to load lazy content
        await page.evaluate('''
            async function scroll() {
                let totalHeight = 0;
                const distance = 200;
                const scrollHeight = document.body.scrollHeight;
                while (totalHeight < scrollHeight) {
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    await new Promise(r => setTimeout(r, 200));
                }
                window.scrollTo(0, 0);
            }
            await scroll();
        ''')
        
        # Load lazy images
        await page.evaluate('''
            const images = document.querySelectorAll('img[data-src], img[lazy-src]');
            for (const img of images) {
                if (img.dataset.src) img.src = img.dataset.src;
                if (img.dataset.lazySrc) img.src = img.dataset.lazySrc;
            }
            await new Promise(r => setTimeout(r, 500));
        ''')
        
        filename = sanitize_filename(url)
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
        return None, str(e), 0
    finally:
        if page:
            await page.close()
        if browser:
            await browser.close()

# ========== TELEGRAM HANDLERS ==========

# مهم: استفاده از filters برای جلوگیری از تداخل
@events.register(events.NewMessage(pattern='/start$', incoming=True))
async def start_command(event):
    """Handle /start command"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ Unauthorized.")
        return
    
    await event.reply(
        "📄 **Web to PDF Bot**\n\n"
        "Send me any webpage URL and I'll convert it to PDF.\n\n"
        "**Commands:**\n"
        "/start - This message\n"
        "/help - Instructions\n"
        "/status - Bot status\n"
        "/pdf <url> - Convert to PDF",
        parse_mode='markdown'
    )


@events.register(events.NewMessage(pattern='/help$', incoming=True))
async def help_command(event):
    """Handle /help command"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ Unauthorized.")
        return
    
    await event.reply(
        "📖 **How to use:**\n\n"
        "Send a URL directly or use:\n"
        "`/pdf https://example.com`\n\n"
        "The bot will:\n"
        "• Load the page\n"
        "• Scroll to load all content\n"
        "• Convert to PDF\n"
        "• Send you the file\n\n"
        "**Limits:**\n"
        "• Max 50MB per PDF\n"
        "• Some dynamic sites may not work",
        parse_mode='markdown'
    )


@events.register(events.NewMessage(pattern='/status$', incoming=True))
async def status_command(event):
    """Handle /status command"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ Unauthorized.")
        return
    
    chromium_path = find_chromium()
    status = "✅ Ready" if chromium_path and PYPPETEER_AVAILABLE else "⚠️ Not ready"
    
    await event.reply(
        f"**Bot Status**\n\n"
        f"Status: {status}\n"
        f"Chromium: {'✅ Found' if chromium_path else '❌ Not found'}\n"
        f"Pyppeteer: {'✅' if PYPPETEER_AVAILABLE else '❌'}\n"
        f"Auth Users: {len(AUTHORIZED_USERS)}",
        parse_mode='markdown'
    )


@events.register(events.NewMessage(pattern='/pdf', incoming=True))
async def pdf_command(event):
    """Handle /pdf command"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ Unauthorized.")
        return
    
    # Check if this message is already being processed
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    
    try:
        parts = event.raw_text.split(maxsplit=1)
        if len(parts) < 2:
            await event.reply("❌ Please provide a URL.\nExample: `/pdf https://example.com`", parse_mode='markdown')
            return
        
        url = parts[1].strip()
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        await process_pdf_request(event, url)
    finally:
        processing_messages.discard(msg_id)


@events.register(events.NewMessage(incoming=True))
async def handle_message(event):
    """Handle regular messages (URLs only)"""
    user_id = event.sender_id
    
    # Skip commands
    if event.raw_text.startswith('/'):
        return
    
    if not is_authorized(user_id):
        return
    
    # Check if this message is already being processed
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    
    # Extract URL
    text = event.raw_text.strip()
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text)
    
    if not urls:
        return
    
    processing_messages.add(msg_id)
    
    try:
        url = urls[0]
        await process_pdf_request(event, url)
    finally:
        processing_messages.discard(msg_id)


async def process_pdf_request(event, url: str):
    """Process PDF conversion request"""
    # Send initial status
    status_msg = await event.reply(f"🔄 Converting `{url[:60]}`...\nThis may take 30-60 seconds.", parse_mode='markdown')
    
    try:
        # Convert to PDF
        filepath, error, file_size = await html_to_pdf(url)
        
        if error or not filepath:
            await safe_edit(status_msg, f"❌ Error: {error[:100]}")
            return
        
        file_size_mb = file_size / (1024 * 1024)
        
        # Send the PDF
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=f"📄 **PDF Ready!**\n📦 {file_size_mb:.2f} MB",
            force_document=True,
            parse_mode='markdown'
        )
        
        # Cleanup
        try:
            os.remove(filepath)
        except:
            pass
        
        # Delete status message
        await status_msg.delete()
        
    except RPCError as e:
        error_msg = str(e)
        if "FILE_PARTS_INVALID" in error_msg:
            await safe_edit(status_msg, "❌ File too large. Try a smaller page.")
        elif "FLOOD_WAIT" in error_msg:
            await safe_edit(status_msg, "⚠️ Too many requests. Please wait.")
        else:
            await safe_edit(status_msg, f"❌ Error: {error_msg[:100]}")
    except Exception as e:
        logger.error(f"Process error: {e}")
        await safe_edit(status_msg, f"❌ Error: {str(e)[:100]}")


async def safe_edit(message, new_text):
    """Safely edit a message without triggering 'Message not modified' error"""
    try:
        await message.edit(new_text, parse_mode='markdown')
    except MessageNotModifiedError:
        # Message content is the same, ignore
        pass
    except Exception as e:
        logger.warning(f"Edit failed: {e}")


# ========== MAIN ==========
async def main():
    print("\n" + "="*50)
    print("📄 TELEGRAM PDF BOT")
    print("="*50)
    
    chromium_path = find_chromium()
    if chromium_path:
        print(f"✅ Chromium: {chromium_path}")
    else:
        print("⚠️ Chromium not found")
    
    print(f"✅ Auth users: {len(AUTHORIZED_USERS)}")
    print("🤖 Starting...\n")
    
    client = TelegramClient('pdf_bot_session', API_ID, API_HASH)
    
    try:
        await client.start(bot_token=BOT_TOKEN)
        
        # Add all event handlers
        client.add_event_handler(start_command)
        client.add_event_handler(help_command)
        client.add_event_handler(status_command)
        client.add_event_handler(pdf_command)
        client.add_event_handler(handle_message)
        
        me = await client.get_me()
        print(f"✅ Bot: @{me.username}")
        print("🎉 Ready!\n")
        
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Fatal: {e}")
        raise
    finally:
        await client.disconnect()


if __name__ == '__main__':
    start_keep_alive()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Stopped")
    except Exception as e:
        print(f"Fatal: {e}")
        sys.exit(1)
