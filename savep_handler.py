import asyncio
import glob
import logging
import os
import time
from urllib.parse import quote

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)

_BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-software-rasterizer",
    "--disable-extensions",
]
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def _find_chrome():
    for pattern in [
        r"C:\Users\Administrator\AppData\Local\ms-playwright\chromium-*\chrome-win\chrome.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]:
        m = glob.glob(pattern)
        if m:
            return m[0]
    return ""


async def _download_file(url, filepath, progress_cb):
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.savethevideo.com/",
    }
    try:
        timeout = ClientTimeout(connect=30, sock_read=120, total=1800)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}", 0
                content_length = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                start_time = time.time()
                last_update = 0
                async with aiofiles.open(filepath, "wb") as f:
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
                                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                                text = (
                                    f"📥 **Downloading...**\n`[{bar}]`\n"
                                    f"💾 {downloaded / 1024 / 1024:.1f}/{content_length / 1024 / 1024:.1f} MB"
                                    f"  •  ⚡ {speed / 1024 / 1024:.1f} MB/s\n📊 {pct:.1f}%"
                                )
                            else:
                                text = (
                                    f"📥 **Downloading...**\n"
                                    f"💾 {downloaded / 1024 / 1024:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
                                )
                            await progress_cb(text)
                return True, None, os.path.getsize(filepath)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[SAVEP] Download error: {e}")
        return False, str(e)[:150], 0


async def _async_extract_savep_v2(video_url, progress_cb, stop_event=None):
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
            browser = await p.chromium.launch(**kw)
            ctx = await browser.new_context(
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )

            page = await ctx.new_page()

            stv_url = f"https://www.savethevideo.com/home?url={quote(video_url)}"
            progress_cb("🌐 Opening savethevideo.com...")
            await page.goto(stv_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            progress_cb("✅ Page loaded")

            for pg in list(ctx.pages):
                if pg is not page:
                    try:
                        await pg.close()
                    except Exception:
                        pass

            # Click Start
            progress_cb("🖱 Clicking Start...")
            start_clicked = False
            try:
                start_btn = page.locator("button").filter(has_text="Start").first
                if await start_btn.is_visible(timeout=10000):
                    await start_btn.click()
                    start_clicked = True
                    progress_cb("✅ Start clicked")
            except Exception:
                pass

            if not start_clicked:
                try:
                    result = await page.evaluate(
                        """() => {
                        for (const b of document.querySelectorAll("button")) {
                            if (b.textContent.trim() === "Start") {
                                b.click();
                                return true;
                            }
                        }
                        return false;
                    }"""
                    )
                    if result:
                        start_clicked = True
                        progress_cb("✅ Start clicked (JS)")
                except Exception:
                    pass

            if not start_clicked:
                return ["❌ Start button not found"]

            # صبر بیشتر تا صفحه کاملاً re-render بشه
            await page.wait_for_timeout(8000)

            for pg in list(ctx.pages):
                if pg is not page:
                    try:
                        await pg.close()
                    except Exception:
                        pass

            # صبر اضافه بعد از بستن پاپ‌آپ‌ها
            await page.wait_for_timeout(2000)

            # Click Convert tab
            progress_cb("🖱 Clicking Convert tab...")
            convert_clicked = False

            # سلکتورهای مختلف برای تب Convert
            convert_selectors = [
                'li[role="tab"]',
                'a[role="tab"]',
                '.nav-tabs li',
                '.tabs li',
                '[data-tab]',
                '.tab',
                'button.tab',
                '.nav-link',
            ]

            for selector in convert_selectors:
                if convert_clicked:
                    break
                try:
                    els = page.locator(selector)
                    count = await els.count()
                    for i in range(count):
                        el = els.nth(i)
                        txt = (await el.text_content() or "").strip()
                        if "convert" in txt.lower():
                            await el.scroll_into_view_if_needed()
                            await el.click()
                            convert_clicked = True
                            progress_cb(f"✅ Convert tab clicked ({selector})")
                            break
                except Exception:
                    pass

            # JS fallback — همه المنت‌ها رو چک کن
            if not convert_clicked:
                try:
                    result = await page.evaluate(
                        """() => {
                        const candidates = document.querySelectorAll(
                            'li, a, button, div, span'
                        );
                        for (const el of candidates) {
                            const t = (el.textContent || '').trim().toLowerCase();
                            if (t === 'convert' || t === 'convert tab') {
                                el.click();
                                return el.tagName + ':' + el.className;
                            }
                        }
                        return null;
                    }"""
                    )
                    if result:
                        convert_clicked = True
                        progress_cb(f"✅ Convert tab clicked (JS fallback: {result})")
                except Exception:
                    pass

            # اگه هیچ‌کدوم کار نکرد، لیست همه تب‌ها رو لاگ کن
            if not convert_clicked:
                try:
                    tabs_info = await page.evaluate(
                        """() => {
                        const results = [];
                        const candidates = document.querySelectorAll(
                            'li, a[role="tab"], button[role="tab"], .nav-link, .tab-item'
                        );
                        for (const el of candidates) {
                            const t = (el.textContent || '').trim();
                            if (t && t.length < 50) {
                                results.push(el.tagName + '[' + (el.role || el.className || '') + ']: ' + t);
                            }
                        }
                        return results.slice(0, 15).join(' | ');
                    }"""
                    )
                    progress_cb(f"🔍 Available elements: {tabs_info[:200]}")
                except Exception:
                    pass

                # آخرین تلاش: select_option
                try:
                    await page.select_option("#tabs", value="1")
                    convert_clicked = True
                    progress_cb("✅ Convert tab via select#tabs")
                except Exception:
                    pass

            if not convert_clicked:
                return ["❌ Could not switch to Convert tab"]

            await page.wait_for_timeout(3000)

            # Wait for convert panel
            try:
                await page.wait_for_selector("#convert-format", timeout=15000)
                progress_cb("✅ Convert panel loaded")
            except Exception:
                pass

            progress_cb("🎬 Selecting MP4 format...")

            # Step 1: Click the select to open the dropdown
            mp4_selected = False
            try:
                cf = page.locator("#convert-format")
                await cf.wait_for(state="visible", timeout=5000)
                await cf.click()
                await page.wait_for_timeout(1000)
                progress_cb("✅ Dropdown opened")
            except Exception:
                pass

            # Step 2: Click the MP4 option
            try:
                mp4_opt = page.locator('#convert-format option[value="mp4"]')
                await mp4_opt.wait_for(state="attached", timeout=5000)
                await mp4_opt.click()
                mp4_selected = True
                progress_cb("✅ MP4 option clicked")
            except Exception:
                pass

            # Step 3: fallback to select_option if click didnt work
            if not mp4_selected:
                try:
                    await page.select_option("#convert-format", value="mp4")
                    mp4_selected = True
                    progress_cb("✅ MP4 selected via select_option")
                except Exception:
                    pass

            if not mp4_selected:
                return ["❌ Could not select MP4 format"]

            await page.wait_for_timeout(1500)

            # Click Convert to MP4 button
            progress_cb("🖱 Clicking Convert to MP4...")
            conv_clicked = False

            try:
                convert_btn = page.locator("a").filter(has_text="Convert to MP4").first
                if await convert_btn.is_visible(timeout=5000):
                    await convert_btn.click()
                    conv_clicked = True
                    progress_cb("✅ Convert to MP4 clicked")
            except Exception:
                pass

            if not conv_clicked:
                try:
                    result = await page.evaluate(
                        """() => {
                        for (const el of document.querySelectorAll("a, button")) {
                            const t = el.textContent.trim();
                            if (t === "Convert to MP4") {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }"""
                    )
                    if result:
                        conv_clicked = True
                        progress_cb("✅ Convert to MP4 clicked (JS)")
                except Exception:
                    pass

            if not conv_clicked:
                return ["❌ Could not click Convert to MP4"]

            await page.wait_for_timeout(4000)

            for pg in list(ctx.pages):
                if pg is not page:
                    try:
                        await pg.close()
                        progress_cb("🚫 Closed popup tab")
                    except Exception:
                        pass

            # Monitor conversion progress
            progress_cb("⏳ Conversion started, monitoring progress...")
            last_status = ""
            rd = 0

            while True:
                if stop_event is not None and stop_event.is_set():
                    return ["❌ Stopped by user"]

                await page.wait_for_timeout(3000)

                for pg in list(ctx.pages):
                    if pg is not page:
                        try:
                            await pg.close()
                        except Exception:
                            pass

                try:
                    status = await page.evaluate(
                        """() => {
                        for (const el of document.querySelectorAll("p, span, div")) {
                            const t = el.textContent.trim();
                            if (t && t.length < 200) {
                                if (/Step\\s+\\d+\\s+of\\s+\\d+/i.test(t)) return t;
                                if (/finished.*click.*download/i.test(t)) return t;
                                if (/Downloading.*\\d+\\.?\\d*%/.test(t)) return t;
                                if (/Downloaded.*\\d+\\.?\\d*%/.test(t)) return t;
                                if (/Processing.*\\d+\\.?\\d*%/.test(t)) return t;
                            }
                        }
                        return "";
                    }"""
                    )
                    if status and status != last_status:
                        last_status = status
                        progress_cb(f"⚙️ {status}")

                    dl_link = await page.evaluate(
                        """() => {
                        for (const a of document.querySelectorAll("a[href]")) {
                            const h = a.href || "";
                            if (h.length < 30) continue;
                            if (h.includes("javascript") || h.includes("utm_") || h.includes("aliexpress") || h.includes("videoproc")) continue;
                            if (h.includes(".mp4") && h.length > 30) return h;
                            const cls = a.className || "";
                            if (cls.includes("bg-blue") && h.length > 30) return h;
                        }
                        return null;
                    }"""
                    )
                    if dl_link:
                        progress_cb("✅ Download link found!")
                        return [dl_link]

                    txt = (status or last_status).lower()
                    if "finished" in txt or "click to download" in txt:
                        progress_cb("🔍 Finished, scanning for link...")
                        await page.wait_for_timeout(3000)
                        fallback = await page.evaluate(
                            """() => {
                            for (const a of document.querySelectorAll("a[href]")) {
                                const h = a.href || "";
                                if (h.length < 30 || h.includes("javascript") || h.includes("utm_")) continue;
                                if (h.includes(".mp4")) return h;
                                if (h.length > 40) return h;
                            }
                            return null;
                        }"""
                        )
                        if fallback:
                            return [fallback]
                        progress_cb("⚠️ Finished but no link, continuing...")
                except Exception as e:
                    progress_cb(f"⚠️ Round {rd + 1}: {str(e)[:80]}")

                rd += 1
                elapsed = rd * 3
                if rd >= 120:
                    progress_cb("❌ Timeout waiting for conversion")
                    break

            return ["❌ Download link not found"]

        except Exception as e:
            progress_cb(f"❌ Error: {str(e)[:200]}")
            return [f"ERROR: {str(e)[:250]}"]
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass


async def process_savep_request(event, url, safe_edit_fn, send_file_fn, download_dir):
    status_msg = await event.reply("🔄 Starting extraction...", parse_mode="markdown")

    async def update_status(text):
        try:
            await safe_edit_fn(status_msg, text)
        except Exception:
            pass

    progress_log = []

    def sync_progress_cb(msg):
        progress_log.append(msg)
        logger.info(f"[SAVEP] {msg}")

    async def live_progress_loop():
        while True:
            await asyncio.sleep(4)
            if progress_log:
                text = (
                    "🔄 **Extracting...**\n```\n"
                    + "\n".join(progress_log[-4:])
                    + "\n```"
                )
                try:
                    await safe_edit_fn(status_msg, text)
                except Exception:
                    pass

    progress_task = asyncio.create_task(live_progress_loop())

    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        await update_status("🌐 **Opening browser...**")
        links = await _async_extract_savep_v2(url, sync_progress_cb)
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    if not links or links[0].startswith("❌") or links[0].startswith("ERROR"):
        err_detail = links[0] if links else "No links found"
        log_text = "\n".join(progress_log[-6:]) if progress_log else "No log"
        await update_status(
            f"❌ **Extraction failed**\n`{err_detail}`\n\n**Last log:**\n```\n{log_text}\n```"
        )
        return

    direct_url = links[0]
    logger.info(f"[SAVEP] Got link: {direct_url[:120]}")
    await update_status("✅ **Link found!**\n\n📥 Starting download...")

    filename = f"savep_{event.chat_id}_{int(time.time())}.mp4"
    filepath = os.path.join(download_dir, filename)

    async def progress_text_cb(text):
        try:
            await safe_edit_fn(status_msg, text)
        except Exception:
            pass

    success, dl_error, final_size = await _download_file(
        direct_url, filepath, progress_text_cb
    )

    if not success or not os.path.exists(filepath):
        await update_status(f"❌ **Download failed:** `{dl_error}`")
        return
    if final_size < 1024:
        await update_status(f"❌ **Download failed:** File too small ({final_size}B)")
        try:
            os.remove(filepath)
        except Exception:
            pass
        return

    await update_status("📤 **Uploading video...**")
    try:
        caption = (
            f"🎬 **Video Downloaded**\n"
            f"📦 Size: `{final_size / 1024 / 1024:.1f} MB`\n"
            f"🔗 [Source]({url})\n"
            f"⬇️ [DW Link]({direct_url})"
        )
        await send_file_fn(
            client=event.client,
            chat_id=event.chat_id,
            filepath=filepath,
            caption=caption,
            status_msg=status_msg,
            buttons=None,
            supports_streaming=True,
        )
    except Exception as e:
        logger.error(f"[SAVEP] Upload error: {e}", exc_info=True)
        await update_status(f"❌ **Upload failed:** `{str(e)[:120]}`")
    finally:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
