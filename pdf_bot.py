#!/usr/bin/env python3
# Telegram PDF & HTML Bot - Save webpage as HTML with live links

import asyncio
import os
import re
import sys
import logging
import glob
import zipfile
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

# ========== SETTINGS ==========
BOT_TOKEN = "7675664254:AAHL7QhPonc47z0QKRFnB5p_L15SRiLBddc"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

AUTHORIZED_USERS = {818185073, 6936101187, 7972834913}

MAX_FILE_SIZE_MB = 50
HTML_TIMEOUT = 60
OUTPUT_FOLDER = "output_files"
HEALTH_PORT = int(os.environ.get('PORT', 10000))

os.makedirs(OUTPUT_FOLDER, exist_ok=True)

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

# ========== FIND CHROMIUM ==========
def find_chromium():
    possible_paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            logger.info(f"✅ Chromium found: {path}")
            return path
    
    home = os.path.expanduser("~")
    pyppeteer_paths = glob.glob(f"{home}/.local/share/pyppeteer/local-chromium/*/chrome-linux/chrome")
    if pyppeteer_paths:
        logger.info(f"✅ Pyppeteer Chromium: {pyppeteer_paths[0]}")
        return pyppeteer_paths[0]
    
    logger.error("❌ Chromium not found!")
    return None

# ========== PDF CONVERSION ==========
async def html_to_pdf(url: str, status_msg=None) -> tuple:
    if not PYPPETEER_AVAILABLE:
        return None, "pyppeteer not installed", 0
    
    chromium_path = find_chromium()
    if not chromium_path:
        return None, "Chromium not found", 0
    
    browser = None
    try:
        if status_msg:
            await safe_edit(status_msg, "🌌 Launching browser...")
        
        browser = await launch(
            headless=True,
            executablePath=chromium_path,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
            defaultViewport={'width': 1280, 'height': 800}
        )
        
        page = await browser.newPage()
        await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        if status_msg:
            await safe_edit(status_msg, f"📄 Loading: {url[:50]}...")
        
        await page.goto(url, {'waitUntil': 'networkidle0', 'timeout': HTML_TIMEOUT * 1000})
        
        # Simple scroll
        try:
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight);')
            await asyncio.sleep(1)
            await page.evaluate('window.scrollTo(0, 0);')
        except:
            pass
        
        if status_msg:
            await safe_edit(status_msg, "📝 Generating PDF...")
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"pdf_{timestamp}.pdf"
        filepath = os.path.join(OUTPUT_FOLDER, filename)
        
        await page.pdf({
            'path': filepath,
            'format': 'A4',
            'printBackground': True,
            'margin': {'top': '15px', 'bottom': '15px', 'left': '15px', 'right': '15px'}
        })
        
        file_size = os.path.getsize(filepath)
        
        if file_size > MAX_FILE_SIZE_MB * 1024 * 1024:
            os.remove(filepath)
            return None, f"PDF too large", 0
        
        return filepath, None, file_size
        
    except Exception as e:
        logger.error(f"PDF error: {e}")
        return None, f"Error: {str(e)[:100]}", 0
    finally:
        if browser:
            await browser.close()

# ========== HTML CAPTURE (با لینک‌های زنده) ==========
async def capture_html(url: str, status_msg=None) -> tuple:
    """Capture webpage as HTML with live links"""
    if not PYPPETEER_AVAILABLE:
        return None, "pyppeteer not installed", 0
    
    chromium_path = find_chromium()
    if not chromium_path:
        return None, "Chromium not found", 0
    
    browser = None
    try:
        if status_msg:
            await safe_edit(status_msg, "🌌 Launching browser...")
        
        browser = await launch(
            headless=True,
            executablePath=chromium_path,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
            defaultViewport={'width': 1280, 'height': 800}
        )
        
        page = await browser.newPage()
        await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        if status_msg:
            await safe_edit(status_msg, f"📄 Loading: {url[:50]}...")
        
        await page.goto(url, {'waitUntil': 'networkidle0', 'timeout': HTML_TIMEOUT * 1000})
        
        # Scroll to load all content
        try:
            await page.evaluate('window.scrollTo(0, document.body.scrollHeight);')
            await asyncio.sleep(2)
            await page.evaluate('window.scrollTo(0, 0);')
            await asyncio.sleep(1)
        except:
            pass
        
        if status_msg:
            await safe_edit(status_msg, "💾 Capturing HTML...")
        
        # Get full HTML content
        html_content = await page.content()
        
        # Get page title
        title = await page.title()
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title[:50]) if title else "webpage"
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Save as HTML file
        html_filename = f"html_{safe_title}_{timestamp}.html"
        html_filepath = os.path.join(OUTPUT_FOLDER, html_filename)
        
        with open(html_filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        html_size = os.path.getsize(html_filepath)
        
        # Create ZIP if HTML is large (Telegram has 50MB limit)
        if html_size > 40 * 1024 * 1024:  # If larger than 40MB, compress
            zip_filename = f"html_{safe_title}_{timestamp}.zip"
            zip_filepath = os.path.join(OUTPUT_FOLDER, zip_filename)
            
            with zipfile.ZipFile(zip_filepath, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(html_filepath, html_filename)
            
            os.remove(html_filepath)
            return zip_filepath, None, os.path.getsize(zip_filepath), True
        
        return html_filepath, None, html_size, False
        
    except Exception as e:
        logger.error(f"HTML capture error: {e}")
        return None, f"Error: {str(e)[:100]}", 0, False
    finally:
        if browser:
            await browser.close()

# ========== HELPER FUNCTIONS ==========
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
    
    status_msg = await event.reply(f"🔄 Converting to PDF...", parse_mode='markdown')
    
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        filepath, error, size = await html_to_pdf(url, status_msg)
        
        if error or not filepath:
            await safe_edit(status_msg, f"❌ {error}")
            return
        
        size_mb = size / (1024 * 1024)
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=f"📄 **PDF Ready!**\n📦 {size_mb:.2f} MB\n🔗 {url[:50]}",
            force_document=True,
            parse_mode='markdown'
        )
        
        try:
            os.remove(filepath)
        except:
            pass
        
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"Process error: {e}")
        await safe_edit(status_msg, f"❌ Error: {str(e)[:100]}")
    finally:
        processing_messages.discard(msg_id)

async def process_html_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    
    status_msg = await event.reply(f"🔄 Capturing HTML with live links...", parse_mode='markdown')
    
    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        filepath, error, size, is_zip = await capture_html(url, status_msg)
        
        if error or not filepath:
            await safe_edit(status_msg, f"❌ {error}")
            return
        
        size_mb = size / (1024 * 1024)
        file_type = "ZIP" if is_zip else "HTML"
        
        caption = (
            f"📄 **HTML with Live Links!**\n"
            f"📦 {size_mb:.2f} MB ({file_type})\n"
            f"🔗 {url[:50]}\n\n"
            f"✅ **All links are clickable!**\n"
            f"💡 Open in browser to browse the page."
        )
        
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=caption,
            force_document=True,
            parse_mode='markdown'
        )
        
        try:
            os.remove(filepath)
        except:
            pass
        
        await status_msg.delete()
        
    except Exception as e:
        logger.error(f"HTML error: {e}")
        await safe_edit(status_msg, f"❌ Error: {str(e)[:100]}")
    finally:
        processing_messages.discard(msg_id)

# ========== TELEGRAM HANDLERS ==========
@events.register(events.NewMessage(pattern='/start', incoming=True))
async def start_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        await event.reply("⛔ Unauthorized")
        return
    
    chromium = find_chromium()
    status = "✅ Ready" if chromium else "❌ Not installed"
    
    await event.reply(
        f"📄 **Web Capture Bot**\n\n"
        f"Save any webpage as:\n"
        f"• `/pdf` - PDF format (static)\n"
        f"• `/html` - HTML with **live clickable links** ✨\n\n"
        f"**Status:** {status}\n"
        f"**Commands:** /help, /status",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/help', incoming=True))
async def help_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    await event.reply(
        "**Commands:**\n"
        "• `/pdf https://example.com` - Save as PDF\n"
        "• `/html https://example.com` - Save as HTML (links work!)\n"
        "• Send URL directly - Default to PDF\n\n"
        "**HTML is better for:**\n"
        "✅ Clickable links\n"
        "✅ Copy text easily\n"
        "✅ Next page navigation\n"
        "✅ Original layout",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/status', incoming=True))
async def status_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    
    chromium = find_chromium()
    await event.reply(
        f"**Bot Status**\n\n"
        f"• Chromium: {'✅ Found' if chromium else '❌ Not found'}\n"
        f"• Pyppeteer: {'✅' if PYPPETEER_AVAILABLE else '❌'}\n"
        f"• Max File Size: {MAX_FILE_SIZE_MB}MB",
        parse_mode='markdown'
    )

@events.register(events.NewMessage(pattern='/pdf', incoming=True))
async def pdf_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        await event.reply("⛔ Unauthorized")
        return
    
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usage: `/pdf https://example.com`", parse_mode='markdown')
        return
    
    await process_pdf_request(event, parts[1].strip())

@events.register(events.NewMessage(pattern='/html', incoming=True))
async def html_cmd(event):
    if event.sender_id not in AUTHORIZED_USERS:
        await event.reply("⛔ Unauthorized")
        return
    
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Usage: `/html https://example.com`", parse_mode='markdown')
        return
    
    await process_html_request(event, parts[1].strip())

@events.register(events.NewMessage(incoming=True))
async def url_handler(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    
    if event.raw_text.startswith('/'):
        return
    
    urls = re.findall(r'https?://[^\s<>"{}|\\^`\[\]]+', event.raw_text)
    if not urls:
        return
    
    # Default to PDF for direct URLs
    await process_pdf_request(event, urls[0])

# ========== MAIN ==========
async def main():
    print("\n" + "="*50)
    print("📄 TELEGRAM PDF & HTML BOT")
    print("="*50)
    
    chromium_path = find_chromium()
    if chromium_path:
        print(f"✅ Chromium: {chromium_path}")
    else:
        print("❌ Chromium NOT FOUND!")
    
    print("🤖 Starting bot...\n")
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    
    await client.start(bot_token=BOT_TOKEN)
    
    client.add_event_handler(start_cmd)
    client.add_event_handler(help_cmd)
    client.add_event_handler(status_cmd)
    client.add_event_handler(pdf_cmd)
    client.add_event_handler(html_cmd)  # جدید
    client.add_event_handler(url_handler)
    
    me = await client.get_me()
    print(f"✅ Bot: @{me.username}")
    print(f"🎉 Ready! Commands: /pdf, /html, /help\n")
    
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
