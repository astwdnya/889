import asyncio
import re
import os
import sys
import time
import base64
import logging
import aiohttp
import aiofiles
from playwright.async_api import async_playwright

logger = logging.getLogger("Y2Mate")

OUTPUT_FOLDER = "output_files"
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


class Y2MateSession:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.qualities = []
        self.download_url = ""
        self.title_text = ""
        self.screenshot_b64 = ""
        self._captured_dl_urls = []
        self._captured_dl_url = None
        self._iframe = None
        self._buttons = []

    async def start_browser(self):
        self.playwright = await async_playwright().__aenter__()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        )
        await self.context.grant_permissions(["clipboard-read", "clipboard-write"])
        self.page = await self.context.new_page()

        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.page.on("popup", lambda p: asyncio.ensure_future(self._handle_popup(p)))

    async def _handle_popup(self, popup):
        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=15000)
            pu = popup.url
            if pu and pu.startswith("http"):
                self._captured_dl_urls.append(pu)
                self._captured_dl_url = pu
            await popup.close()
        except Exception:
            try:
                await popup.close()
            except Exception:
                pass

    async def _on_request(self, request):
        url = request.url
        if "yt-dl.click" in url or "yt-dl.com" in url or ".yt-dl." in url:
            self._captured_dl_urls.append(url)
            self._captured_dl_url = url

    async def _on_response(self, response):
        url = str(response.url)
        if "yt-dl.click" in url or "yt-dl.com" in url or ".yt-dl." in url:
            if url not in self._captured_dl_urls:
                self._captured_dl_urls.append(url)
                self._captured_dl_url = url

    async def close_browser(self):
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                await self.playwright.__aexit__(None, None, None)
        except Exception:
            pass

    async def take_screenshot(self):
        try:
            ss = await self.page.screenshot(full_page=True, type="png")
            self.screenshot_b64 = base64.b64encode(ss).decode()
        except Exception:
            pass

    async def navigate_to_y2mate(self):
        await self.page.goto(
            "https://v21.www-y2mate.com/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await asyncio.sleep(2)

    async def paste_url(self, video_url: str):
        input_sel = "input.y2mate_query.keyword"
        await self.page.wait_for_selector(input_sel, timeout=30000)
        await self.page.fill(input_sel, "")
        await self.page.type(input_sel, video_url, delay=50)
        await asyncio.sleep(2)

        start_btn = self.page.locator("div.converter-btn")
        try:
            await start_btn.wait_for(timeout=5000)
            await start_btn.click()
        except Exception:
            pass

        try:
            await self.page.wait_for_url("**/convert/**", timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(3)

    async def extract_title(self):
        try:
            el = await self.page.query_selector("h3.video-title")
            if el:
                self.title_text = (await el.inner_text()).strip()
                logger.info(
                    f"extract_title: found via h3.video-title -> {self.title_text[:60]}"
                )
                return
        except Exception as e:
            logger.warning(f"extract_title: h3.video-title failed: {e}")
        try:
            el = await self.page.query_selector(".video-title")
            if el:
                self.title_text = (await el.inner_text()).strip()
                logger.info(
                    f"extract_title: found via .video-title -> {self.title_text[:60]}"
                )
                return
        except Exception as e:
            logger.warning(f"extract_title: .video-title failed: {e}")
        try:
            el = await self.page.query_selector("h3")
            if el:
                txt = (await el.inner_text()).strip()
                if txt and len(txt) > 5:
                    self.title_text = txt
                    logger.info(
                        f"extract_title: found via h3 -> {self.title_text[:60]}"
                    )
                    return
        except Exception as e:
            logger.warning(f"extract_title: h3 failed: {e}")
        try:
            self.title_text = (await self.page.title()).strip()
            logger.info(
                f"extract_title: found via page.title -> {self.title_text[:60]}"
            )
        except Exception as e:
            logger.warning(f"extract_title: page.title failed: {e}")
        if not self.title_text:
            logger.warning("extract_title: ALL methods failed, title is empty")

    async def _get_iframe(self):
        try:
            for f in self.page.frames:
                if "y2meta-uk.com" in f.url or "wwwindex.php" in f.url:
                    self._iframe = f
                    return f
        except Exception:
            pass
        iframe_el = await self.page.query_selector("iframe#widgetv2Api")
        if iframe_el:
            f = await iframe_el.content_frame()
            self._iframe = f
            return f
        return None

    async def click_video_tab(self):
        iframe = await self._get_iframe()
        if not iframe:
            return
        try:
            video_tab = iframe.locator('a.nav-link[data-tab="mp4"]')
            await video_tab.wait_for(timeout=15000)
            await video_tab.click()
            await asyncio.sleep(2)
        except Exception:
            pass

    async def parse_qualities(self):
        self.qualities = []
        self._buttons = []
        iframe = await self._get_iframe()
        if not iframe:
            return self.qualities

        try:
            await iframe.wait_for_selector("table.table", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(1)

        rows = await iframe.query_selector_all("table.table > tbody > tr")
        for row in rows:
            tds = await row.query_selector_all("td")
            if len(tds) < 2:
                continue
            label_text = await tds[0].inner_text()
            label = label_text.strip()

            if not label or "p" not in label.lower():
                continue

            btn = await row.query_selector("button[data-note]")
            if not btn:
                continue
            note = await btn.get_attribute("data-note") or ""
            fmt = await btn.get_attribute("data-format") or "mp4"

            size_text = ""
            if len(tds) > 1:
                size_text = await tds[1].inner_text()
            size_text = size_text.strip()

            self.qualities.append(
                {
                    "label": label,
                    "note": note,
                    "format": fmt,
                    "size": size_text,
                }
            )
            self._buttons.append(btn)
        return self.qualities

    async def select_quality(self, index: int) -> dict:
        if index < 0 or index >= len(self.qualities):
            return {"success": False, "error": "Invalid index"}

        self._captured_dl_url = None
        self._captured_dl_urls = []
        q = self.qualities[index]

        iframe = await self._get_iframe()
        if iframe:
            tab = "mp4" if q["format"] == "mp4" else "mp3"
            try:
                await iframe.evaluate(f'''
                    document.querySelector('a.nav-link[data-tab="{tab}"]')?.click()
                ''')
                await asyncio.sleep(3)
            except Exception:
                try:
                    tab_link = iframe.locator(f'a.nav-link[data-tab="{tab}"]')
                    await tab_link.wait_for(timeout=5000)
                    await tab_link.click()
                    await asyncio.sleep(3)
                except Exception:
                    pass

        iframe = await self._get_iframe()
        if not iframe:
            return {"success": False, "error": "Iframe lost"}
        await asyncio.sleep(1)

        try:
            clicked = await iframe.evaluate(f'''
                (() => {{
                    const btn = document.querySelector(
                        'button[data-note="{q["note"]}"][data-format="{q["format"]}"]'
                    );
                    if (!btn) return false;
                    btn.click();
                    return true;
                }})()
            ''')
            if not clicked:
                return {"success": False, "error": "Quality button not found in iframe"}
        except Exception:
            return {"success": False, "error": "Quality button not found in iframe"}
        await asyncio.sleep(3)

        iframe = await self._get_iframe()
        if not iframe:
            return {"success": False, "error": "Iframe disappeared"}

        dl_link = iframe.locator("a.btn-download-link")
        try:
            await dl_link.wait_for(timeout=45000)
        except Exception:
            return {"success": False, "error": "Download link in modal did not appear"}

        await asyncio.sleep(2)

        try:
            dl_span = dl_link.locator("span")
            await dl_span.click()
        except Exception:
            try:
                await dl_link.click()
            except Exception:
                pass
        await asyncio.sleep(4)

        current_url = self.page.url
        if "yt-dl.click" in current_url or "yt-dl." in current_url:
            self._captured_dl_urls.append(current_url)
            self._captured_dl_url = current_url

        for url in self._captured_dl_urls:
            if "yt-dl.click" in url or "yt-dl." in url:
                self.download_url = url
                return {"success": True, "download_url": url}

        for _ in range(15):
            current_url = self.page.url
            if "yt-dl.click" in current_url or "yt-dl." in current_url:
                if current_url not in self._captured_dl_urls:
                    self._captured_dl_urls.append(current_url)
                    self._captured_dl_url = current_url
            for url in self._captured_dl_urls:
                if "yt-dl.click" in url or "yt-dl." in url:
                    self.download_url = url
                    return {"success": True, "download_url": url}
            await asyncio.sleep(1)

        return {"success": False, "error": "Could not get download URL"}

    async def run_full_flow(self, video_url: str) -> dict:
        steps = []
        try:
            steps.append("Starting browser...")
            await self.start_browser()
            steps.append("Navigating to y2mate.com...")
            await self.navigate_to_y2mate()
            steps.append("Pasting URL...")
            await self.paste_url(video_url)
            steps.append("Extracting title...")
            await self.extract_title()
            steps.append("Clicking Video tab...")
            await self.click_video_tab()
            steps.append("Parsing qualities...")
            await self.parse_qualities()
            steps.append(f"Found {len(self.qualities)} quality options")
            return {
                "success": True,
                "qualities": [
                    {
                        "index": i,
                        "label": q["label"],
                        "size": q.get("size", ""),
                        "format": q["format"],
                    }
                    for i, q in enumerate(self.qualities)
                ],
                "steps": steps,
                "session": self,
            }
        except Exception as e:
            await self.take_screenshot()
            await self.close_browser()
            return {
                "success": False,
                "error": str(e),
                "steps": steps,
                "screenshot_b64": self.screenshot_b64,
                "session": self,
            }


async def download_with_resume(url: str, filepath: str):
    CHUNK = 2 * 1024 * 1024
    dl_headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://v21.www-y2mate.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Connection": "keep-alive",
    }
    connector = aiohttp.TCPConnector(ssl=False, limit=8, force_close=True)
    timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=180)
    downloaded = 0
    no_progress_retries = 0
    max_no_progress = 3

    while no_progress_retries < max_no_progress:
        h = dl_headers.copy()
        if downloaded > 0:
            h["Range"] = f"bytes={downloaded}-"
        last_print = time.time()

        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as s:
                async with s.get(url, headers=h, allow_redirects=True) as resp:
                    if resp.status == 416:
                        print(f"  ✅ Already complete ({downloaded} bytes)")
                        return
                    if resp.status not in (200, 206):
                        print(f"  HTTP {resp.status} — keeping {downloaded} bytes")
                        return
                    before = downloaded
                    if resp.status == 200 and downloaded > 0:
                        downloaded = 0
                        mode = "wb"
                    elif resp.status == 206 and downloaded > 0:
                        mode = "ab"
                    else:
                        mode = "wb"
                    async with aiofiles.open(filepath, mode) as f:
                        try:
                            async for chunk in resp.content.iter_chunked(CHUNK):
                                await f.write(chunk)
                                downloaded += len(chunk)
                                now = time.time()
                                if now - last_print >= 2.0:
                                    print(
                                        f"  Downloaded {downloaded // 1024 // 1024}MB"
                                    )
                                    last_print = now
                        except aiohttp.ClientPayloadError as ce:
                            print(f"\n  ⚠️ Partial {downloaded} bytes — {ce}")
                    if downloaded == before:
                        no_progress_retries += 1
                    else:
                        no_progress_retries = 0
        except (OSError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"\n  ⚠️ Connection error: {e}")
            no_progress_retries += 1
            await asyncio.sleep(2)

    print(f"  Reached max retries. Got {downloaded} bytes total.")


async def main():
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("Enter YouTube URL: ").strip()

    if not url:
        print("No URL provided.")
        return

    session = Y2MateSession()
    result = await session.run_full_flow(url)

    if not result["success"]:
        print(f"Error: {result.get('error', 'Unknown')}")
        ss = result.get("screenshot_b64", "")
        if ss:
            ss_path = f"error_ss_{int(time.time())}.png"
            with open(ss_path, "wb") as f:
                f.write(base64.b64decode(ss))
            print(f"Screenshot saved to {ss_path}")
        await session.close_browser()
        return

    qualities = result["qualities"]
    print(f"\nFound {len(qualities)} quality options:\n")
    for q in qualities:
        sz = f" ({q['size']})" if q.get("size") else ""
        print(f"  {q['index']}. {q['label']}{sz}")

    print()
    choice = input("Select quality number: ").strip()
    try:
        idx = int(choice)
    except ValueError:
        print("Invalid number.")
        await session.close_browser()
        return

    dl_result = await session.select_quality(idx)
    if not dl_result["success"]:
        print(f"Download failed: {dl_result.get('error', 'Unknown')}")
        await session.close_browser()
        return

    dl_url = dl_result["download_url"]
    print(f"\nDownload URL: {dl_url}")

    print("\nDownloading...")
    filepath = os.path.join(OUTPUT_FOLDER, f"y2mate_{int(time.time())}.mp4")
    await download_with_resume(dl_url, filepath)
    size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
    if size > 1024:
        print(f"✅ Download complete: {size // 1024 // 1024}MB -> {filepath}")
    else:
        print(f"⚠️ Got only {size} bytes — might not be a video.")

    await session.close_browser()


if __name__ == "__main__":
    asyncio.run(main())
