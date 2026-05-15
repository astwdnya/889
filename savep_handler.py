"""
savep_handler.py — standalone, no savep.py needed
"""
import asyncio, glob, logging, os, re, time
from urllib.parse import quote
import aiofiles, aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)

_BROWSER_ARGS = [
    '--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
    '--disable-software-rasterizer','--disable-extensions',
    '--disable-background-networking','--disable-sync',
    '--disable-translate','--hide-scrollbars','--mute-audio','--no-first-run',
]
_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

async def _find_chrome():
    for pattern in [
        r"C:\Users\Administrator\AppData\Local\ms-playwright\chromium-*\chrome-win\chrome.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]:
        m = glob.glob(pattern)
        if m: return m[0]
    return ""

def _is_youtube_url(url):
    return bool(re.search(r'(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)', url, re.IGNORECASE))


# ── savethevideo.com extractor ────────────────────────────────────────────────
async def _async_extract_savep(video_url, progress_cb, stop_event=None):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ["ERROR: playwright not installed"]

    async with async_playwright() as p:
        browser = None
        try:
            exe = await _find_chrome()
            kw = dict(headless=True, args=_BROWSER_ARGS)
            if exe:
                kw["executable_path"] = exe
                progress_cb(f"🖥 Browser: {exe[-55:]}")
            else:
                progress_cb("🖥 Using default playwright browser...")

            browser = await p.chromium.launch(**kw)
            ctx = await browser.new_context(user_agent=_USER_AGENT, viewport={'width':1280,'height':800}, locale='en-US')

            stv_url = f"https://www.savethevideo.com/downloader?url={quote(video_url)}"
            progress_cb("🌐 Opening savethevideo...")
            page = await ctx.new_page()
            await page.goto(stv_url, wait_until='domcontentloaded', timeout=60000)
            progress_cb("✅ Page loaded")
            await page.wait_for_timeout(3000)

            # کلیک Start
            progress_cb("🖱 Clicking Start...")
            started = False
            for sel in ['button:has-text("Start")', 'button.bg-gray-800']:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=5000):
                        await el.click(); started = True
                        progress_cb("✅ Start clicked"); break
                except Exception: continue
            if not started:
                try:
                    await page.evaluate('() => { for(const b of document.querySelectorAll("button")) if(b.textContent.trim()==="Start"){b.click();break;} }')
                    progress_cb("✅ Start clicked (JS)")
                except Exception:
                    progress_cb("⚠️ Start not found — continuing anyway")

            progress_cb("⏳ Waiting 5s...")
            await page.wait_for_timeout(5000)

            for pg in list(ctx.pages):
                if pg is page: continue
                try: await pg.close(); progress_cb("🚫 Closed popup")
                except Exception: pass

            try: _ = await page.title()
            except Exception:
                progress_cb("⚠️ Main page lost — reopening...")
                page = await ctx.new_page()
                await page.goto(stv_url, wait_until='domcontentloaded', timeout=60000)
                await page.wait_for_timeout(3000)

            # صبر برای tabs
            for _ in range(10):
                try:
                    count = await page.evaluate("() => document.querySelectorAll('li[role=\"tab\"]').length")
                    if count >= 2: progress_cb(f"✅ {count} tabs ready"); break
                except Exception: pass
                await page.wait_for_timeout(1500)

            # Convert tab
            progress_cb("🖱 Clicking Convert tab...")
            convert_clicked = False
            for sel in ['#react-tabs-2', 'li[aria-controls="react-tabs-3"]', 'li[role="tab"]:has-text("Convert")']:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        await el.click(); convert_clicked = True
                        progress_cb("✅ Convert tab clicked"); break
                except Exception: continue
            if not convert_clicked:
                result = await page.evaluate('''() => {
                    const byId = document.getElementById("react-tabs-2");
                    if (byId) { byId.click(); return "by-id"; }
                    const byAria = document.querySelector("li[aria-controls=\\"react-tabs-3\\"]");
                    if (byAria) { byAria.click(); return "by-aria"; }
                    for (const li of document.querySelectorAll("li[role=\\"tab\\"]"))
                        if (li.textContent.trim() === "Convert") { li.click(); return "by-text"; }
                    return "NOT_FOUND";
                }''')
                if result and not result.startswith("NOT_FOUND"):
                    progress_cb("✅ Convert tab clicked (JS)"); convert_clicked = True

            await page.wait_for_timeout(1000)
            try:
                await page.wait_for_selector('#convert-format', state='visible', timeout=8000)
                progress_cb("✅ Convert panel loaded")
            except Exception:
                progress_cb("⚠️ Convert panel not visible — trying anyway")
                await page.wait_for_timeout(2000)

            # انتخاب MP4
            progress_cb("🎬 Selecting MP4...")
            try: await page.select_option('#convert-format', value='mp4')
            except Exception: pass
            try:
                val = await page.evaluate('''() => {
                    const s = document.getElementById("convert-format");
                    if (!s) return "NOT_FOUND";
                    s.value = "mp4";
                    s.dispatchEvent(new Event("change", {bubbles:true}));
                    s.dispatchEvent(new Event("input", {bubbles:true}));
                    return s.value;
                }''')
                progress_cb(f"✅ Format: {val}")
            except Exception: pass

            await page.wait_for_timeout(2000)

            # کلیک Convert to MP4
            progress_cb("🖱 Clicking Convert to MP4...")
            conv_ok = False
            for sel in ['a:has-text("Convert to MP4")', 'a:has-text("Convert to mp4")']:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        await el.dispatch_event('click'); conv_ok = True
                        progress_cb("✅ Convert clicked"); break
                except Exception: continue
            if not conv_ok:
                result = await page.evaluate('''() => {
                    for (const el of document.querySelectorAll("a"))
                        if (/convert to mp4/i.test(el.textContent.trim())) {
                            el.dispatchEvent(new MouseEvent("click",{bubbles:true,cancelable:true}));
                            return "clicked";
                        }
                    return "NOT_FOUND";
                }''')
                if result and result != "NOT_FOUND": conv_ok = True

            if not conv_ok:
                return ["❌ نتونست روی Convert to MP4 کلیک کنه"]

            # صبر برای لینک + نمایش progress کانورت
            progress_cb("⏳ Conversion started...")
            found_link = None; last_status = ""; rd = 0

            while True:
                if stop_event is not None and stop_event.is_set():
                    return ["❌ Stopped by user"]
                await page.wait_for_timeout(3000)
                try:
                    status = await page.evaluate('''() => {
                        for (const el of document.querySelectorAll("p")) {
                            const t = el.textContent.trim();
                            if (!t || t.length > 200 || t.length < 3) continue;
                            if (/Step\\s+\\d+\\s+of\\s+\\d+/i.test(t)) return t;
                            if (/Finished/i.test(t)) return t;
                            if (/click\\s+to\\s+download/i.test(t)) return t;
                            if (/\\d+(\\.\\d+)?\\s*%/.test(t)) return t;
                        }
                        for (const el of document.querySelectorAll("span,div,h3,h4")) {
                            if (el.children.length > 3) continue;
                            const t = el.textContent.trim();
                            if (!t || t.length > 150 || t.length < 5) continue;
                            if (/Step\\s+\\d+\\s+of\\s+\\d+/i.test(t)) return t;
                            if (/Finished/i.test(t) && t.length < 80) return t;
                            if (/click\\s+to\\s+download/i.test(t)) return t;
                        }
                        return "";
                    }''')
                    if status and status != last_status:
                        last_status = status; progress_cb(f"⚙️ {status}")

                    dl_link = await page.evaluate('''() => {
                        for (const a of document.querySelectorAll("a[href]")) {
                            const cls = a.className || ""; const h = a.href || "";
                            if (cls.includes("bg-blue") && h.length > 30 && !h.endsWith("#") &&
                                !h.includes("javascript") && !h.includes("utm_") && !h.includes("videoproc"))
                                return h;
                        }
                        for (const a of document.querySelectorAll("a[href]")) {
                            const h = a.href || "";
                            if (h.includes(".mp4") && !h.includes("utm_") && !h.includes("videoproc") &&
                                !h.includes("javascript") && (h.includes("savethevideo") || h.includes(".v02.") || /\\/generic-\\d+/.test(h)))
                                return h;
                        }
                        return null;
                    }''')
                    if dl_link:
                        found_link = dl_link; progress_cb("✅ Download link found!"); break

                    txt = (status or last_status).lower()
                    if "finished" in txt or "click to download" in txt:
                        progress_cb("🔍 Finished — final scan...")
                        await page.wait_for_timeout(2000)
                        fallback = await page.evaluate('''() => {
                            for (const a of document.querySelectorAll("a[href]")) {
                                const h = a.href || ""; const cls = a.className || "";
                                if (!h || h.length < 30 || h.endsWith("#") || h.includes("utm_") || h.includes("javascript")) continue;
                                if (cls.includes("bg-blue")) return h;
                            }
                            return null;
                        }''')
                        if fallback:
                            found_link = fallback; progress_cb("✅ Link found (fallback)!"); break

                except Exception as e:
                    progress_cb(f"⚠️ Round {rd+1}: {e}")

                rd += 1
                elapsed = rd * 3
                mins, secs = divmod(elapsed, 60)
                if rd % 20 == 0:
                    progress_cb(f"⏳ Still converting... ({mins}m {secs}s)")

            if found_link: return [found_link]
            return ["❌ لینکی پیدا نشد"]

        except Exception as e:
            progress_cb(f"❌ Error: {str(e)[:200]}")
            return [f"ERROR: {str(e)[:250]}"]
        finally:
            if browser:
                try: await browser.close()
                except Exception: pass


# ── ytdown.to extractor (YouTube) ─────────────────────────────────────────────
async def _async_extract_ytdown(video_url, progress_cb):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return ["ERROR: playwright not installed"]

    async with async_playwright() as p:
        browser = None
        try:
            exe = await _find_chrome()
            kw = dict(headless=True, args=_BROWSER_ARGS)
            if exe: kw["executable_path"] = exe
            browser = await p.chromium.launch(**kw)
            ctx = await browser.new_context(user_agent=_USER_AGENT, viewport={'width':1280,'height':800}, locale='en-US')

            captured_links = []
            _YT_CDN_PATTERNS = ['iamworker.com', '/v5/download/']

            async def handle_route(route, request):
                url = request.url
                if any(pat in url for pat in _YT_CDN_PATTERNS):
                    captured_links.append(url)
                    progress_cb(f"🎯 Captured: {url[:120]}")
                    await route.abort()
                else:
                    await route.continue_()

            progress_cb("🌐 Opening ytdown.to...")
            page = await ctx.new_page()
            await page.goto("https://app.ytdown.to/en27/", wait_until='domcontentloaded', timeout=60000)
            progress_cb("✅ Page loaded")
            await page.wait_for_timeout(2000)
            await ctx.route('**/*', handle_route)

            await ctx.grant_permissions(['clipboard-read', 'clipboard-write'])
            try:
                await page.evaluate(f"async () => {{ await navigator.clipboard.writeText({repr(video_url)}); }}")
            except Exception: pass

            paste_clicked = False
            for sel in ['button.paste-button', 'button[aria-label="Paste"]', 'button:has-text("Paste")']:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=5000):
                        await el.click(); paste_clicked = True
                        progress_cb("✅ Paste clicked"); break
                except Exception: continue

            if not paste_clicked:
                for input_sel in ['input[type="text"]', 'input[type="url"]', 'input', 'textarea']:
                    try:
                        inp = page.locator(input_sel).first
                        if await inp.is_visible(timeout=3000):
                            await inp.fill(video_url)
                            progress_cb("✅ URL filled"); break
                    except Exception: continue

            await page.wait_for_timeout(2000)

            for sel in ['div.download-label', '.download-label']:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=6000):
                        await el.dispatch_event('click')
                        progress_cb("✅ Download clicked"); break
                except Exception: continue

            await page.wait_for_timeout(2000)

            for sel in ['div.download-container.btn-download', '.btn-download', 'div.btn-download']:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=6000):
                        await el.dispatch_event('click')
                        progress_cb("✅ Final Download clicked"); break
                except Exception: continue

            progress_cb("⏳ Waiting for link...")
            for rd in range(15):
                await page.wait_for_timeout(2000)
                if captured_links:
                    progress_cb("✅ YouTube link captured!")
                    return [captured_links[-1]]
                for pg in list(ctx.pages):
                    if pg is not page:
                        try: await pg.close()
                        except Exception: pass

            if captured_links: return [captured_links[-1]]
            return ["❌ YouTube link not captured"]

        except Exception as e:
            progress_cb(f"❌ YTDown error: {str(e)[:200]}")
            return [f"ERROR: {str(e)[:250]}"]
        finally:
            if browser:
                try: await browser.close()
                except Exception: pass


# ── دانلود فایل با progress ───────────────────────────────────────────────────
async def _download_file(url, filepath, progress_cb_text):
    headers = {'User-Agent': _USER_AGENT, 'Referer': 'https://www.savethevideo.com/'}
    try:
        timeout = ClientTimeout(connect=30, sock_read=120, total=1800)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}", 0
                content_length = int(resp.headers.get('Content-Length', 0))
                downloaded = 0; start_time = time.time(); last_update = 0
                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(512 * 1024):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_update >= 2.0:
                            last_update = now
                            elapsed = now - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            if content_length > 0:
                                pct = downloaded / content_length * 100
                                bar = "█" * int(pct/5) + "░" * (20 - int(pct/5))
                                text = (f"📥 **Downloading...**\n`[{bar}]`\n"
                                        f"💾 {downloaded/1024/1024:.1f}/{content_length/1024/1024:.1f} MB"
                                        f"  •  ⚡ {speed/1024/1024:.1f} MB/s\n📊 {pct:.1f}%")
                            else:
                                text = (f"📥 **Downloading...**\n"
                                        f"💾 {downloaded/1024/1024:.1f} MB  •  ⚡ {speed/1024/1024:.1f} MB/s")
                            await progress_cb_text(text)
                return True, None, os.path.getsize(filepath)
    except asyncio.CancelledError: raise
    except Exception as e:
        logger.error(f"[SAVEP] Download error: {e}")
        return False, str(e)[:150], 0


# ── هندلر اصلی /savep ────────────────────────────────────────────────────────
async def process_savep_request(event, url, safe_edit_fn, send_file_fn, download_dir="/tmp"):
    status_msg = await event.reply("🔄 Starting extraction...", parse_mode='markdown')

    async def update_status(text):
        try: await safe_edit_fn(status_msg, text)
        except Exception: pass

    progress_log = []

    def sync_progress_cb(msg):
        progress_log.append(msg)
        logger.info(f"[SAVEP] {msg}")

    async def live_progress_loop():
        while True:
            await asyncio.sleep(4)
            if progress_log:
                text = "🔄 **Extracting...**\n```\n" + "\n".join(progress_log[-4:]) + "\n```"
                try: await safe_edit_fn(status_msg, text)
                except Exception: pass

    progress_task = asyncio.create_task(live_progress_loop())

    try:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        await update_status("🌐 **Opening browser...**\n_This may take 30-60 seconds_")
        loop = asyncio.get_event_loop()

        if _is_youtube_url(url):
            sync_progress_cb("🎬 YouTube detected — using ytdown.to")
            links = await loop.run_in_executor(None, lambda: asyncio.run(_async_extract_ytdown(url, sync_progress_cb)))
        else:
            links = await loop.run_in_executor(None, lambda: asyncio.run(_async_extract_savep(url, sync_progress_cb)))

    finally:
        progress_task.cancel()
        try: await progress_task
        except asyncio.CancelledError: pass

    if not links or links[0].startswith("❌") or links[0].startswith("ERROR"):
        err_detail = links[0] if links else "No links found"
        log_text = "\n".join(progress_log[-6:]) if progress_log else "No log"
        await update_status(f"❌ **Extraction failed**\n`{err_detail}`\n\n**Last log:**\n```\n{log_text}\n```")
        return

    direct_url = links[0]
    logger.info(f"[SAVEP] Got link: {direct_url[:120]}")
    await update_status("✅ **Link found!**\n\n📥 Starting download...")

    filename = f"savep_{event.chat_id}_{int(time.time())}.mp4"
    filepath = os.path.join(download_dir, filename)

    async def progress_text_cb(text):
        try: await safe_edit_fn(status_msg, text)
        except Exception: pass

    success, dl_error, final_size = await _download_file(direct_url, filepath, progress_text_cb)

    if not success or not os.path.exists(filepath):
        await update_status(f"❌ **Download failed:** `{dl_error}`"); return
    if final_size == 0:
        await update_status("❌ **Download failed:** File is empty")
        try: os.remove(filepath)
        except Exception: pass
        return

    await update_status("📤 **Uploading video...**")
    try:
        caption = (f"🎬 **Video Downloaded**\n"
                   f"📦 Size: `{final_size/1024/1024:.1f} MB`\n"
                   f"🔗 [Source]({url})\n"
                   f"⬇️ [DW Link]({direct_url})")
        await send_file_fn(client=event.client, chat_id=event.chat_id, filepath=filepath,
                           caption=caption, status_msg=status_msg, buttons=None, supports_streaming=True)
    except Exception as e:
        logger.error(f"[SAVEP] Upload error: {e}", exc_info=True)
        await update_status(f"❌ **Upload failed:** `{str(e)[:120]}`")
    finally:
        try:
            if os.path.exists(filepath): os.remove(filepath)
        except Exception: pass
