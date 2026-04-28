#!/usr/bin/env python3
# Telegram PDF Bot - Convert any webpage to PDF with images & GIFs
# Uses Telethon + Flask for keep-alive

import asyncio
import os
import re
import sys
import logging
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

# Flask for keep-alive
from flask import Flask
from threading import Thread

# Telegram
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument
from telethon.errors import RPCError

# PDF & Web requirements
try:
    from pyppeteer import launch
    PYPPETEER_AVAILABLE = True
except ImportError:
    PYPPETEER_AVAILABLE = False
    print("⚠️ pyppeteer not installed. Run: pip install pyppeteer")

# ========== CONFIGURATION (Direct in code) ==========
# Telegram Bot Configuration
BOT_TOKEN = "7675664254:AAHL7QhPonc47z0QKRFnB5p_L15SRiLBddc"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

# Authorized users (only these can use the bot)
AUTHORIZED_USERS = {818185073, 6936101187, 7972834913}

# File settings
MAX_FILE_SIZE_MB = 2000  # 2GB max
CLEANUP_DELAY_SECONDS = 20

# PDF specific settings
PDF_FOLDER = "pdf_output"
PDF_TIMEOUT = 60  # seconds for page load
MAX_PDF_SIZE_MB = 50  # Telegram limit for non-premium (actually 50MB)

# Flask keep-alive settings
HEALTH_PORT = 10000

# Chromium path (auto-detect or set manually)
CHROMIUM_PATH = None  # Auto-detect, or set like: "/usr/bin/chromium"

# ========== LOGGING ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== CREATE FOLDERS ==========
os.makedirs(PDF_FOLDER, exist_ok=True)

# ========== FLASK KEEP-ALIVE ==========
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "✅ PDF Bot is running!", 200

@flask_app.route('/health')
def health():
    return {"status": "ok", "time": datetime.now().isoformat()}, 200

def run_flask():
    """Run Flask server in a separate thread"""
    try:
        flask_app.run(host='0.0.0.0', port=HEALTH_PORT, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Flask error: {e}")

def start_keep_alive():
    """Start Flask keep-alive server"""
    thread = Thread(target=run_flask, daemon=True)
    thread.start()
    logger.info(f"🌐 Keep-alive server started on port {HEALTH_PORT}")

# ========== HELPER FUNCTIONS ==========
def is_authorized(user_id: int) -> bool:
    """Check if user is authorized to use the bot"""
    return user_id in AUTHORIZED_USERS

def sanitize_filename(url: str) -> str:
    """Create safe filename from URL"""
    parsed = urlparse(url)
    domain = parsed.netloc.replace('.', '_')
    path = re.sub(r'[^\w\-_]', '_', parsed.path[:50]) if parsed.path else 'home'
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"pdf_{domain}_{path}_{timestamp}.pdf"
    # Remove any remaining invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    return filename

def find_chromium():
    """Find Chromium executable path"""
    global CHROMIUM_PATH
    
    if CHROMIUM_PATH and os.path.exists(CHROMIUM_PATH):
        return CHROMIUM_PATH
    
    # Common paths for different systems
    common_paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/snap/bin/chromium",
        os.path.expanduser("~/.local/share/pyppeteer/local-chromium/*/chrome-linux/chrome"),
        os.path.expanduser("~/.cache/pyppeteer/local-chromium/*/chrome-linux/chrome"),
    ]
    
    for path in common_paths:
        import glob
        expanded = glob.glob(path)
        if expanded:
            return expanded[0]
        if os.path.exists(path):
            return path
    
    return None

# ========== PDF CONVERSION FUNCTION ==========
async def html_to_pdf(url: str, status_message=None) -> tuple:
    """
    Convert webpage to PDF preserving all content (images, GIFs, layout)
    Returns: (filepath, error_message, file_size_bytes)
    """
    
    if not PYPPETEER_AVAILABLE:
        return None, "❌ pyppeteer not installed. Install with: pip install pyppeteer", 0
    
    browser = None
    page = None
    
    try:
        # Find Chromium
        chromium_path = find_chromium()
        if not chromium_path:
            return None, "❌ Chromium not found. Install with: apt-get install chromium", 0
        
        logger.info(f"Using Chromium: {chromium_path}")
        
        if status_message:
            await status_message.edit("🌐 Launching browser...")
        
        # Launch browser
        browser = await launch(
            headless=True,
            executablePath=chromium_path,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-software-rasterizer',
                '--disable-extensions',
                '--disable-web-security',
                '--window-size=1920,1080'
            ],
            handleSIGINT=False,
            handleSIGTERM=False,
            defaultViewport={'width': 1920, 'height': 1080}
        )
        
        if status_message:
            await status_message.edit(f"📄 Loading page: {url[:60]}...")
        
        page = await browser.newPage()
        
        # Set user agent
        await page.setUserAgent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        # Load page
        try:
            response = await page.goto(url, {
                'waitUntil': 'networkidle2',
                'timeout': PDF_TIMEOUT * 1000
            })
            
            if response and response.status >= 400:
                return None, f"❌ HTTP {response.status} error loading page", 0
                
        except Exception as e:
            return None, f"❌ Failed to load page: {str(e)[:100]}", 0
        
        if status_message:
            await status_message.edit("🖼️ Processing images and scrolling...")
        
        # Scroll to bottom to load lazy content
        await page.evaluate('''
            async function scrollToBottom() {
                let totalHeight = 0;
                const distance = 200;
                const scrollHeight = await page.evaluate(() => document.body.scrollHeight);
                
                while (totalHeight < scrollHeight) {
                    await page.evaluate(`window.scrollBy(0, ${distance})`);
                    totalHeight += distance;
                    await new Promise(resolve => setTimeout(resolve, 200));
                }
                
                // Scroll back to top
                await page.evaluate('window.scrollTo(0, 0)');
                await new Promise(resolve => setTimeout(resolve, 500));
            }
            await scrollToBottom();
        ''')
        
        # Wait for lazy images
        await page.evaluate('''
            async function loadLazyImages() {
                const images = document.querySelectorAll('img[data-src], img[lazy-src], img[data-original]');
                for (const img of images) {
                    if (img.dataset.src) img.src = img.dataset.src;
                    if (img.lazySrc) img.src = img.lazySrc;
                    if (img.dataset.original) img.src = img.dataset.original;
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
            }
            await loadLazyImages();
        ''')
        
        if status_message:
            await status_message.edit("📝 Generating PDF...")
        
        # PDF options
        pdf_options = {
            'format': 'A4',
            'printBackground': True,
            'margin': {
                'top': '20px',
                'right': '20px',
                'bottom': '20px',
                'left': '20px'
            },
            'displayHeaderFooter': True,
            'headerTemplate': f'''
                <div style="font-size:8px; width:100%; text-align:center; padding:5px;">
                    {urlparse(url).netloc}
                </div>
            ''',
            'footerTemplate': '''
                <div style="font-size:8px; width:100%; text-align:center; padding:5px;">
                    Page <span class="pageNumber"></span> of <span class="totalPages"></span>
                </div>
            '''
        }
        
        # Create filename and save PDF
        filename = sanitize_filename(url)
        filepath = os.path.join(PDF_FOLDER, filename)
        
        await page.pdf({'path': filepath, **pdf_options})
        
        # Check file size
        file_size = os.path.getsize(filepath)
        file_size_mb = file_size / (1024 * 1024)
        
        if file_size_mb > MAX_PDF_SIZE_MB:
            os.remove(filepath)
            return None, f"❌ PDF size ({file_size_mb:.1f}MB) exceeds {MAX_PDF_SIZE_MB}MB limit", 0
        
        if status_message:
            await status_message.edit(f"✅ PDF created! Size: {file_size_mb:.2f} MB")
        
        return filepath, None, file_size
        
    except Exception as e:
        logger.error(f"PDF conversion error: {e}")
        return None, f"❌ Error: {str(e)[:150]}", 0
        
    finally:
        # Cleanup
        if page:
            try:
                await page.close()
            except:
                pass
        if browser:
            try:
                await browser.close()
            except:
                pass


# ========== TELEGRAM BOT HANDLERS ==========
async def start_command(event):
    """Handle /start command"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ You are not authorized to use this bot.")
        return
    
    await event.reply(
        "📄 **Web to PDF Bot**\n\n"
        "Send me any webpage URL and I'll convert it to PDF with:\n"
        "✅ All images and GIFs\n"
        "✅ Full page scroll\n"
        "✅ Original layout\n\n"
        "**Usage:**\n"
        "`/pdf https://example.com`\n\n"
        "Or just send me a direct URL!\n\n"
        "**Commands:**\n"
        "/start - Show this message\n"
        "/help - Detailed instructions\n"
        "/pdf <url> - Convert webpage to PDF",
        parse_mode='markdown'
    )


async def help_command(event):
    """Handle /help command"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ Unauthorized.")
        return
    
    await event.reply(
        "📖 **How to use:**\n\n"
        "1. Send a webpage URL directly\n"
        "2. Or use: `/pdf https://example.com`\n\n"
        "**What happens:**\n"
        "• Bot loads the page in a headless browser\n"
        "• Scrolls to bottom to load all content\n"
        "• Waits for lazy-loaded images\n"
        "• Converts to PDF with preserved layout\n\n"
        "**Limitations:**\n"
        "• Max PDF size: 50MB\n"
        "• Some dynamic sites may not load fully\n"
        "• Pages with login required won't work\n\n"
        "**Requirements:**\n"
        "• Chromium must be installed on server\n"
        "• Pyppeteer Python package",
        parse_mode='markdown'
    )


async def pdf_command(event):
    """Handle /pdf command"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ You are not authorized.")
        return
    
    # Get URL from command arguments
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply(
            "❌ Please provide a URL.\n"
            "Example: `/pdf https://example.com`",
            parse_mode='markdown'
        )
        return
    
    url = parts[1].strip()
    
    # Validate URL
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    parsed = urlparse(url)
    if not parsed.netloc:
        await event.reply("❌ Invalid URL. Please provide a valid web address.")
        return
    
    await process_pdf_request(event, url)


async def handle_message(event):
    """Handle regular messages (URLs)"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ You are not authorized to use this bot.")
        return
    
    # Check if message contains a URL
    text = event.raw_text.strip()
    
    # Simple URL detection
    url_pattern = r'https?://[^\s]+'
    urls = re.findall(url_pattern, text)
    
    if not urls:
        await event.reply(
            "❌ Please send a valid webpage URL.\n"
            "Example: `https://example.com`\n\n"
            "Use `/help` for more information.",
            parse_mode='markdown'
        )
        return
    
    url = urls[0]
    await process_pdf_request(event, url)


async def process_pdf_request(event, url: str):
    """Process PDF conversion request"""
    # Send initial status
    status_msg = await event.reply(f"🔄 Processing: `{url[:80]}`...", parse_mode='markdown')
    
    # Update status message (Telethon doesn't support edit like python-telegram-bot)
    # We'll send new messages instead
    try:
        # Convert to PDF
        filepath, error, file_size = await html_to_pdf(url, None)
        
        if error or not filepath:
            await status_msg.edit(f"{error}")
            return
        
        # Send the PDF file
        file_size_mb = file_size / (1024 * 1024)
        caption = (
            f"📄 **PDF Ready!**\n"
            f"🌐 {url[:100]}\n"
            f"📦 Size: {file_size_mb:.2f} MB\n"
            f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        # Send document
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=caption,
            force_document=True,
            parse_mode='markdown'
        )
        
        # Cleanup
        os.remove(filepath)
        
        # Delete status message
        await status_msg.delete()
        
    except RPCError as e:
        logger.error(f"Telegram RPC error: {e}")
        await status_msg.edit(f"❌ Failed to send file: {str(e)[:100]}")
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await status_msg.edit(f"❌ Error: {str(e)[:150]}")


async def error_handler(event):
    """Handle errors"""
    logger.error(f"Error: {event}")
    try:
        await event.reply("❌ An unexpected error occurred. Please try again later.")
    except:
        pass


# ========== MAIN BOT ==========
async def main():
    """Main function to run the bot"""
    
    print("\n" + "="*50)
    print("📄 TELEGRAM PDF BOT")
    print("="*50)
    
    # Check pyppeteer
    if not PYPPETEER_AVAILABLE:
        print("\n⚠️ WARNING: pyppeteer not installed!")
        print("Install with: pip install pyppeteer")
        print("Also need Chromium: apt-get install chromium\n")
        
    # Check Chromium
    chromium_path = find_chromium()
    if chromium_path:
        print(f"✅ Chromium found: {chromium_path}")
    else:
        print("\n⚠️ WARNING: Chromium not found!")
        print("Install with: apt-get install chromium\n")
    
    print(f"✅ Authorized users: {len(AUTHORIZED_USERS)}")
    print(f"📁 PDF folder: {PDF_FOLDER}")
    print(f"📦 Max PDF size: {MAX_PDF_SIZE_MB} MB")
    print("\n🤖 Starting bot...\n")
    
    # Create Telegram client
    client = TelegramClient(
        'pdf_bot_session',
        API_ID,
        API_HASH,
        connection_retries=5,
        retry_delay=3
    )
    
    # Start client with bot token
    await client.start(bot_token=BOT_TOKEN)
    
    # Register handlers
    client.add_event_handler(start_command, events.NewMessage(pattern='/start'))
    client.add_event_handler(help_command, events.NewMessage(pattern='/help'))
    client.add_event_handler(pdf_command, events.NewMessage(pattern='/pdf'))
    client.add_event_handler(handle_message, events.NewMessage(incoming=True))
    
    logger.info("✅ Bot started successfully!")
    print("\n🎉 Bot is running!")
    print("Press Ctrl+C to stop...\n")
    
    # Run until interrupted
    await client.run_until_disconnected()


if __name__ == '__main__':
    # Start Flask keep-alive
    start_keep_alive()
    
    # Run bot
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)