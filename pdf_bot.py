#!/usr/bin/env python3
# Telegram PDF Bot - Convert any webpage to PDF with images & GIFs
# Works on Render.com with Chromium installed via Aptfile

import asyncio
import os
import re
import sys
import logging
import glob
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

# Flask for keep-alive
from flask import Flask
from threading import Thread

# Telegram
from telethon import TelegramClient, events
from telethon.errors import RPCError

# PDF & Web requirements
try:
    from pyppeteer import launch
    PYPPETEER_AVAILABLE = True
except ImportError:
    PYPPETEER_AVAILABLE = False
    print("⚠️ pyppeteer not installed. Run: pip install pyppeteer")

# ========== CONFIGURATION ==========
# Read from environment variables (for Render) or use defaults
BOT_TOKEN = os.getenv('BOT_TOKEN', "7675664254:AAHL7QhPonc47z0QKRFnB5p_L15SRiLBddc")
API_ID = int(os.getenv('API_ID', 2040))
API_HASH = os.getenv('API_HASH', "b18441a1ff607e10a989891a5462e627")

# Authorized users (comma-separated in env)
AUTHORIZED_USERS_STR = os.getenv('AUTHORIZED_USERS', "818185073,6936101187,7972834913")
AUTHORIZED_USERS = {int(x.strip()) for x in AUTHORIZED_USERS_STR.split(',')}

# File settings
MAX_PDF_SIZE_MB = int(os.getenv('MAX_PDF_SIZE_MB', 50))  # Telegram limit is 50MB
PDF_TIMEOUT = int(os.getenv('PDF_TIMEOUT', 60))  # seconds
PDF_FOLDER = os.getenv('PDF_FOLDER', "pdf_output")
CLEANUP_DELAY = int(os.getenv('CLEANUP_DELAY', 300))  # 5 minutes

# Flask keep-alive settings
HEALTH_PORT = int(os.getenv('PORT', 10000))

# Chromium path (auto-detected)
CHROMIUM_PATH = os.getenv('CHROMIUM_PATH', None)

# ========== CREATE FOLDERS ==========
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
    """Health check endpoint for Render"""
    return {
        "status": "ok",
        "time": datetime.now().isoformat(),
        "bot": "Telegram PDF Bot",
        "pyppeteer": PYPPETEER_AVAILABLE,
        "chromium": find_chromium() is not None
    }, 200

@flask_app.route('/health')
def health():
    """Detailed health check"""
    chromium_path = find_chromium()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "chromium_installed": chromium_path is not None,
        "chromium_path": chromium_path,
        "pyppeteer_available": PYPPETEER_AVAILABLE,
        "authorized_users": len(AUTHORIZED_USERS),
        "pdf_folder": PDF_FOLDER
    }, 200

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

def find_chromium():
    """Find Chromium executable path"""
    global CHROMIUM_PATH
    
    # Check if already found
    if CHROMIUM_PATH and os.path.exists(CHROMIUM_PATH):
        return CHROMIUM_PATH
    
    # Check environment variable
    env_path = os.getenv('CHROMIUM_PATH')
    if env_path and os.path.exists(env_path):
        CHROMIUM_PATH = env_path
        return CHROMIUM_PATH
    
    # Common paths for different systems
    common_paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chrome",
        "/snap/bin/chromium",
        "/opt/google/chrome/chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",  # Mac
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",  # Windows
    ]
    
    for path in common_paths:
        if os.path.exists(path):
            CHROMIUM_PATH = path
            return CHROMIUM_PATH
    
    # Check pyppeteer's downloaded Chromium
    try:
        home = os.path.expanduser("~")
        pyppeteer_patterns = [
            f"{home}/.local/share/pyppeteer/local-chromium/*/chrome-linux/chrome",
            f"{home}/.cache/pyppeteer/local-chromium/*/chrome-linux/chrome",
            f"{home}/Library/Application Support/pyppeteer/local-chromium/*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        ]
        
        for pattern in pyppeteer_patterns:
            matches = glob.glob(pattern)
            if matches:
                CHROMIUM_PATH = matches[0]
                return CHROMIUM_PATH
    except Exception as e:
        logger.warning(f"Error searching pyppeteer chromium: {e}")
    
    return None

def sanitize_filename(url: str) -> str:
    """Create safe filename from URL"""
    parsed = urlparse(url)
    domain = parsed.netloc.replace('.', '_').replace('-', '_')
    path = re.sub(r'[^\w\-_]', '_', parsed.path[:50]) if parsed.path else 'home'
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"pdf_{domain}_{path}_{timestamp}.pdf"
    # Remove any remaining invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # Limit filename length
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:195] + ext
    return filename

def cleanup_old_files():
    """Clean up old PDF files"""
    try:
        now = datetime.now()
        for filepath in glob.glob(os.path.join(PDF_FOLDER, '*.pdf')):
            if os.path.isfile(filepath):
                file_age = now - datetime.fromtimestamp(os.path.getmtime(filepath))
                if file_age.total_seconds() > CLEANUP_DELAY:
                    try:
                        os.remove(filepath)
                        logger.info(f"Cleaned up old file: {filepath}")
                    except Exception as e:
                        logger.error(f"Error removing {filepath}: {e}")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

# ========== PDF CONVERSION FUNCTION ==========
async def html_to_pdf(url: str, status_message=None) -> tuple:
    """
    Convert webpage to PDF preserving all content (images, GIFs, layout)
    Returns: (filepath, error_message, file_size_bytes)
    """
    
    if not PYPPETEER_AVAILABLE:
        error_msg = "❌ pyppeteer not installed. Install with: pip install pyppeteer"
        if status_message:
            await status_message.edit(error_msg)
        return None, error_msg, 0
    
    chromium_path = find_chromium()
    if not chromium_path:
        error_msg = "❌ Chromium not found. Please install Chromium on the server."
        if status_message:
            await status_message.edit(error_msg)
        return None, error_msg, 0
    
    browser = None
    page = None
    
    try:
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
                '--disable-features=VizDisplayCompositor',
                '--enable-features=NetworkService,NetworkServiceInProcess',
                '--window-size=1920,1080'
            ],
            handleSIGINT=False,
            handleSIGTERM=False,
            handleSIGHUP=False,
            defaultViewport={'width': 1920, 'height': 1080}
        )
        
        if status_message:
            await status_message.edit(f"📄 Loading page...")
        
        page = await browser.newPage()
        
        # Set user agent to avoid blocking
        await page.setUserAgent(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        
        # Set extra headers
        await page.setExtraHTTPHeaders({
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
        })
        
        # Load page
        try:
            response = await page.goto(url, {
                'waitUntil': 'networkidle2',
                'timeout': PDF_TIMEOUT * 1000
            })
            
            if response and response.status >= 400:
                return None, f"❌ HTTP {response.status} error loading page", 0
                
        except asyncio.TimeoutError:
            return None, f"❌ Page load timeout after {PDF_TIMEOUT} seconds", 0
        except Exception as e:
            return None, f"❌ Failed to load page: {str(e)[:100]}", 0
        
        if status_message:
            await status_message.edit("🖼️ Scrolling and loading images...")
        
        # Scroll to bottom to load lazy content
        await page.evaluate('''
            async function scrollToBottom() {
                let totalHeight = 0;
                const distance = 300;
                
                // Get scroll height
                const scrollHeight = await new Promise((resolve) => {
                    let lastHeight = 0;
                    let count = 0;
                    const checkHeight = setInterval(() => {
                        const currentHeight = document.body.scrollHeight;
                        if (currentHeight === lastHeight || count > 20) {
                            clearInterval(checkHeight);
                            resolve(currentHeight);
                        }
                        lastHeight = currentHeight;
                        count++;
                    }, 500);
                });
                
                // Scroll step by step
                while (totalHeight < scrollHeight) {
                    window.scrollBy(0, distance);
                    totalHeight += distance;
                    await new Promise(resolve => setTimeout(resolve, 200));
                }
                
                // Scroll back to top
                window.scrollTo(0, 0);
                await new Promise(resolve => setTimeout(resolve, 500));
            }
            await scrollToBottom();
        ''')
        
        # Load lazy images
        await page.evaluate('''
            async function loadLazyImages() {
                const images = document.querySelectorAll('img[data-src], img[lazy-src], img[data-original], img[data-lazy]');
                for (const img of images) {
                    if (img.dataset.src) img.src = img.dataset.src;
                    if (img.dataset.lazySrc) img.src = img.dataset.lazySrc;
                    if (img.dataset.original) img.src = img.dataset.original;
                    if (img.dataset.lazy) img.src = img.dataset.lazy;
                }
                await new Promise(resolve => setTimeout(resolve, 1000));
            }
            await loadLazyImages();
        ''')
        
        # Trigger any remaining lazy loaders
        await page.evaluate('''
            window.dispatchEvent(new Event('scroll'));
            window.dispatchEvent(new Event('resize'));
            await new Promise(resolve => setTimeout(resolve, 500));
        ''')
        
        if status_message:
            await status_message.edit("📝 Generating PDF...")
        
        # PDF options for best quality
        pdf_options = {
            'format': 'A4',
            'printBackground': True,
            'preferCSSPageSize': False,
            'scale': 1,
            'displayHeaderFooter': True,
            'headerTemplate': f'''
                <div style="font-size:8px; width:100%; text-align:center; padding:5px; color:#666;">
                    {urlparse(url).netloc}
                </div>
            ''',
            'footerTemplate': '''
                <div style="font-size:8px; width:100%; text-align:center; padding:5px; color:#666;">
                    Page <span class="pageNumber"></span> of <span class="totalPages"></span>
                </div>
            ''',
            'margin': {
                'top': '25px',
                'right': '20px',
                'bottom': '25px',
                'left': '20px'
            }
        }
        
        # Create filename and save PDF
        filename = sanitize_filename(url)
        filepath = os.path.join(PDF_FOLDER, filename)
        
        await page.pdf({'path': filepath, **pdf_options})
        
        # Check if file was created
        if not os.path.exists(filepath):
            return None, "❌ PDF file was not created", 0
        
        # Check file size
        file_size = os.path.getsize(filepath)
        file_size_mb = file_size / (1024 * 1024)
        
        if file_size_mb > MAX_PDF_SIZE_MB:
            os.remove(filepath)
            return None, f"❌ PDF size ({file_size_mb:.1f}MB) exceeds {MAX_PDF_SIZE_MB}MB limit", 0
        
        if status_message:
            await status_message.edit(f"✅ PDF created! Size: {file_size_mb:.2f} MB")
        
        logger.info(f"PDF created: {filepath} ({file_size_mb:.2f}MB)")
        return filepath, None, file_size
        
    except asyncio.CancelledError:
        logger.warning("PDF conversion cancelled")
        return None, "❌ Conversion cancelled", 0
        
    except Exception as e:
        logger.error(f"PDF conversion error: {e}", exc_info=True)
        return None, f"❌ Error: {str(e)[:150]}", 0
        
    finally:
        # Cleanup browser resources
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
    
    # Check Chromium status
    chromium_ok = find_chromium() is not None
    
    status_icon = "✅" if chromium_ok else "⚠️"
    chromium_status = "Ready" if chromium_ok else "Not installed (PDF conversion may fail)"
    
    await event.reply(
        f"📄 **Web to PDF Bot**\n\n"
        f"Send me any webpage URL and I'll convert it to PDF with:\n"
        f"✅ All images and GIFs\n"
        f"✅ Full page scroll\n"
        f"✅ Original layout\n\n"
        f"**System Status:**\n"
        f"{status_icon} Chromium: {chromium_status}\n\n"
        f"**Usage:**\n"
        f"`/pdf https://example.com`\n\n"
        f"Or just send me a direct URL!\n\n"
        f"**Commands:**\n"
        f"/start - Show this message\n"
        f"/help - Detailed instructions\n"
        f"/status - Check bot status\n"
        f"/pdf <url> - Convert webpage to PDF",
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
        "**1. Send a webpage URL directly**\n"
        "Just paste any URL like: `https://example.com`\n\n"
        "**2. Use the /pdf command**\n"
        "`/pdf https://example.com`\n\n"
        "**What happens behind the scenes:**\n"
        "• Bot loads the page in a headless browser\n"
        "• Scrolls to bottom to load all content\n"
        "• Waits for lazy-loaded images and GIFs\n"
        "• Converts to PDF with preserved layout\n"
        "• Sends you the PDF file\n\n"
        "**Limitations:**\n"
        "• Max PDF size: 50MB (Telegram limit)\n"
        "• Some dynamic sites may not load fully\n"
        "• Pages with login/captcha won't work\n"
        "• Very long pages may take 30-60 seconds\n\n"
        "**Technical Requirements:**\n"
        "• Chromium must be installed on server\n"
        "• Pyppeteer Python package\n\n"
        "**Need help?** Contact the bot administrator.",
        parse_mode='markdown'
    )


async def status_command(event):
    """Handle /status command - check bot health"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ Unauthorized.")
        return
    
    chromium_path = find_chromium()
    chromium_ok = chromium_path is not None
    
    status_text = f"""
📊 **Bot Status Report**

**System:**
• Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}
• Pyppeteer: {'✅ Installed' if PYPPETEER_AVAILABLE else '❌ Not installed'}
• Chromium: {'✅ Found' if chromium_ok else '❌ Not found'}
{f'  └─ Path: {chromium_path}' if chromium_ok else ''}

**Configuration:**
• Authorized users: {len(AUTHORIZED_USERS)}
• Max PDF size: {MAX_PDF_SIZE_MB} MB
• PDF timeout: {PDF_TIMEOUT} seconds
• PDF folder: {PDF_FOLDER}

**Status:** {'🟢 Ready' if chromium_ok and PYPPETEER_AVAILABLE else '🔴 Not ready'}
"""
    
    await event.reply(status_text, parse_mode='markdown')


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
    await process_pdf_request(event, url)


async def handle_message(event):
    """Handle regular messages (URLs)"""
    user_id = event.sender_id
    
    if not is_authorized(user_id):
        await event.reply("⛔ You are not authorized to use this bot.")
        return
    
    # Check if message contains a URL
    text = event.raw_text.strip()
    
    # URL detection pattern
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    urls = re.findall(url_pattern, text)
    
    if not urls:
        # Ignore non-URL messages
        return
    
    url = urls[0]
    await process_pdf_request(event, url)


async def process_pdf_request(event, url: str):
    """Process PDF conversion request"""
    user_id = event.sender_id
    
    # Validate URL format
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            await event.reply("❌ Invalid URL. Please provide a valid web address.")
            return
    except Exception:
        await event.reply("❌ Invalid URL format.")
        return
    
    # Clean up old files before starting
    cleanup_old_files()
    
    # Send initial status
    status_msg = await event.reply(f"🔄 Converting `{url[:80]}`...\n\nThis may take 30-60 seconds.", parse_mode='markdown')
    
    try:
        # Convert to PDF
        filepath, error, file_size = await html_to_pdf(url, status_msg)
        
        if error or not filepath:
            await status_msg.edit(f"{error}")
            return
        
        # Send the PDF file
        file_size_mb = file_size / (1024 * 1024)
        caption = (
            f"📄 **PDF Ready!**\n"
            f"🌐 `{url[:100]}`\n"
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
        try:
            os.remove(filepath)
            logger.info(f"Cleaned up: {filepath}")
        except Exception as e:
            logger.warning(f"Could not remove {filepath}: {e}")
        
        # Delete status message
        await status_msg.delete()
        
    except RPCError as e:
        logger.error(f"Telegram RPC error: {e}")
        error_msg = str(e)
        if "FILE_PARTS_INVALID" in error_msg:
            await status_msg.edit("❌ File too large for Telegram. Try a smaller page or lower quality.")
        elif "FLOOD_WAIT" in error_msg:
            await status_msg.edit("⚠️ Too many requests. Please wait a few minutes and try again.")
        else:
            await status_msg.edit(f"❌ Failed to send file: {error_msg[:100]}")
        
    except Exception as e:
        logger.error(f"Unexpected error in process_pdf_request: {e}", exc_info=True)
        await status_msg.edit(f"❌ Unexpected error: {str(e)[:150]}")


async def error_handler(event):
    """Global error handler"""
    logger.error(f"Unhandled error: {event}")
    try:
        if hasattr(event, 'reply'):
            await event.reply("❌ An unexpected error occurred. Please try again later.")
    except:
        pass


# ========== MAIN BOT ==========
async def main():
    """Main function to run the bot"""
    
    print("\n" + "="*60)
    print("📄 TELEGRAM PDF BOT")
    print("="*60)
    
    # Check pyppeteer
    if not PYPPETEER_AVAILABLE:
        print("\n⚠️ WARNING: pyppeteer not installed!")
        print("   Install with: pip install pyppeteer")
    
    # Check Chromium
    chromium_path = find_chromium()
    if chromium_path:
        print(f"✅ Chromium found: {chromium_path}")
    else:
        print("\n⚠️ WARNING: Chromium not found!")
        print("   Install with: apt-get install chromium")
        print("   Or create an Aptfile with 'chromium' in it")
    
    print(f"\n📊 Configuration:")
    print(f"   • Authorized users: {len(AUTHORIZED_USERS)}")
    print(f"   • Max PDF size: {MAX_PDF_SIZE_MB} MB")
    print(f"   • PDF timeout: {PDF_TIMEOUT} seconds")
    print(f"   • PDF folder: {PDF_FOLDER}")
    print(f"   • Health port: {HEALTH_PORT}")
    
    print("\n🤖 Starting Telegram bot...")
    
    # Create Telegram client
    client = TelegramClient(
        'pdf_bot_session',
        API_ID,
        API_HASH,
        connection_retries=5,
        retry_delay=3,
        request_retries=3
    )
    
    try:
        # Start client with bot token
        await client.start(bot_token=BOT_TOKEN)
        
        # Register handlers
        client.add_event_handler(start_command, events.NewMessage(pattern='/start$'))
        client.add_event_handler(help_command, events.NewMessage(pattern='/help$'))
        client.add_event_handler(status_command, events.NewMessage(pattern='/status$'))
        client.add_event_handler(pdf_command, events.NewMessage(pattern='/pdf'))
        client.add_event_handler(handle_message, events.NewMessage(incoming=True))
        
        # Get bot info
        me = await client.get_me()
        print(f"\n✅ Bot started successfully!")
        print(f"   • Bot name: {me.first_name}")
        print(f"   • Bot username: @{me.username}")
        print(f"   • Bot ID: {me.id}")
        
        print("\n🎉 Bot is running and waiting for messages!")
        print("Press Ctrl+C to stop...\n")
        
        # Run until disconnected
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        raise
    finally:
        await client.disconnect()


if __name__ == '__main__':
    # Start Flask keep-alive server (for Render)
    start_keep_alive()
    
    # Run bot
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
