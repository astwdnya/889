import asyncio
import re
import os
import sys
import time
import random
import base64
from playwright.async_api import async_playwright

URL_PATTERN = re.compile(r"https?://[^\s/$.?#].[^\s]*", re.IGNORECASE)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => false });
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' })),
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } } };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);
"""


class SnapWCSession:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.qualities = []
        self.title_text = ""
        self.download_url = ""
        self.captcha_image_b64 = ""
        self.waiting_for_captcha = False
        self.done = False
        self.error = ""
        self.screenshot_b64 = ""

    async def start_browser(self):
        self.playwright = await async_playwright().__aenter__()
        user_agent = random.choice(
            [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ]
        )
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-quic",
                "--disable-setuid-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-popup-blocking",
                "--mute-audio",
            ],
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1.0,
            user_agent=user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            permissions=["clipboard-read", "clipboard-write"],
            color_scheme="light",
        )
        try:
            await self.context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass

        self.page = await self.context.new_page()
        await self.page.add_init_script(STEALTH_JS)

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

    async def take_screenshot(self) -> str:
        try:
            page = getattr(self, "current_page", self.page) or self.page
            if page:
                buf = await page.screenshot(type="png", full_page=True)
                self.screenshot_b64 = base64.b64encode(buf).decode()
                return self.screenshot_b64
        except Exception:
            pass
        return ""

    async def navigate(self):
        last_exc = None
        for retry in range(3):
            try:
                await self.page.goto(
                    "https://snapwc.com/sites",
                    wait_until="domcontentloaded",
                    timeout=90000,
                )
                await asyncio.sleep(2)
                return
            except Exception as e:
                last_exc = e
                if retry < 2:
                    await asyncio.sleep(5)
        raise last_exc or Exception("Navigation failed")

    async def paste_url(self, video_url: str):
        await self.page.wait_for_selector(
            'input[name="video-url-input"]', timeout=30000
        )
        el = self.page.locator('input[name="video-url-input"]')
        await el.click()
        await el.fill("")
        await asyncio.sleep(random.uniform(0.1, 0.3))
        for char in video_url:
            await el.type(char, delay=random.randint(30, 80))

    async def click_get_links(self):
        await self.page.locator('span.block:text("Get Download Links")').first.click(
            timeout=10000
        )

    async def wait_for_conversion(self):
        progress_selector = "div.text-center.text-caption.text-grey"
        last_progress = ""
        for _ in range(180):
            try:
                el = await self.page.query_selector(progress_selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and text != last_progress:
                        last_progress = text
                    if text.rstrip("%") == "100":
                        return
                test_q = await self.page.query_selector_all("div.q-item__label")
                for q_el in test_q:
                    t = (await q_el.inner_text()).strip()
                    if t and (re.search(r"\d{3,4}p", t) or "mp4" in t.lower()):
                        return
            except Exception:
                pass
            await asyncio.sleep(1)

    async def parse_qualities(self):
        self.qualities = []
        for _ in range(120):
            all_labels = await self.page.query_selector_all("div.q-item__label")
            found = []
            for el in all_labels:
                t = (await el.inner_text()).strip()
                if not t:
                    continue
                is_q = (
                    bool(re.search(r"\d{3,4}p", t))
                    or "mp4" in t.lower()
                    or "m4a" in t.lower()
                    or ("kbps" in t.lower() and re.search(r"\d+", t))
                    or "webm" in t.lower()
                )
                if not is_q:
                    continue
                if any(q["label"] == t for q in found):
                    continue

                cat = "Video"
                try:
                    cat_js = await el.evaluate(r"""el => {
                        let sib = el.previousElementSibling;
                        for (let i = 0; i < 20; i++) {
                            if (!sib) {
                                let p = el.parentElement;
                                for (let j = 0; j < 10; j++) {
                                    if (!p) break;
                                    sib = p.previousElementSibling;
                                    if (sib && sib.classList.contains('text-subtitle1')) break;
                                    p = p.parentElement;
                                }
                                if (!sib) break;
                            }
                            if (sib.classList.contains('text-subtitle1')) {
                                const iconEl = sib.querySelector('i');
                                const icon = iconEl ? iconEl.textContent.trim() : '';
                                if (icon === 'volume_off') return 'No Sound';
                                if (icon === 'audiotrack') return 'Audio';
                                return 'Video';
                            }
                            sib = sib.previousElementSibling;
                        }
                        return 'Video';
                    }""")
                    cat = cat_js if cat_js else "Video"
                except Exception:
                    pass

                size = ""
                try:
                    size_el = await el.evaluate_handle(r"""el => {
                        let sib = el.nextElementSibling;
                        if (sib && sib.classList.contains('q-item__label--caption')) return sib;
                        let parent = el.parentElement;
                        if (parent) {
                            const cap = parent.querySelector('.q-item__label--caption');
                            if (cap) return cap;
                        }
                        return null;
                    }""")
                    if size_el:
                        size_elem = size_el.as_element()
                        if size_elem:
                            size = (await size_elem.inner_text()).strip()
                except Exception:
                    pass

                found.append({"label": t, "category": cat, "size": size})

            if found:
                self.qualities = found
                return
            await asyncio.sleep(1)

        raise Exception("No quality options found")

    async def _human_click(self, element, page=None):
        p = page or self.page
        box = await element.bounding_box()
        if box:
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await p.mouse.move(
                box["x"] + box["width"] * 0.1,
                box["y"] + box["height"] * 0.1,
                steps=random.randint(5, 12),
            )
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await p.mouse.move(x, y, steps=random.randint(8, 20))
            await asyncio.sleep(random.uniform(0.05, 0.15))
            await p.mouse.click(x, y)
        else:
            await element.click()

    async def _click_download_icon_near(self, quality_el):
        """Find file_download icon near a quality element and click it with human-like motion."""
        dl_handle = await quality_el.evaluate_handle("""el => {
            let parent = el.parentElement;
            const findIcon = (p) => {
                const icons = p.querySelectorAll('i.q-icon');
                for (const ic of icons) {
                    if (ic.textContent.trim() === 'file_download') return ic;
                }
                return null;
            };
            let icon = findIcon(el.parentElement || el);
            if (icon) return icon;
            for (let i = 0; i < 10; i++) {
                if (!parent) break;
                icon = findIcon(parent);
                if (icon) return icon;
                parent = parent.parentElement;
            }
            return null;
        }""")
        dl_elem = dl_handle.as_element() if dl_handle else None
        if dl_elem:
            await self._human_click(dl_elem)
        else:
            await self._human_click(quality_el)

    async def select_and_click_download(self, index: int):
        if index < 0 or index >= len(self.qualities):
            raise ValueError(f"Invalid quality index: {index}")

        label = self.qualities[index]["label"]
        quality_el = self.page.locator(f'div.q-item__label:text("{label}")').first

        await quality_el.wait_for(timeout=10000)

        for attempt in range(3):
            try:
                async with self.page.context.expect_page(timeout=12000) as pi:
                    await self._click_download_icon_near(quality_el)

                popup = await pi.value
                await popup.wait_for_load_state("load", timeout=15000)
                pu = popup.url
                if "offer-support" in pu or "supportsnapwc" in pu:
                    await popup.close()
                    await asyncio.sleep(2)
                    try:
                        async with self.page.context.expect_page(timeout=15000):
                            pass
                        real = await pi.value
                        # pi is consumed, need new context
                        # Instead just wait and check current pages
                    except Exception:
                        pass
                    # After closing fake popup, check if download appears in-page
                    await asyncio.sleep(3)
                    has_dl = await self.page.locator(
                        'button:has-text("Copy Download Link"), span:has-text("Copy Download Link")'
                    ).first.is_visible()
                    if has_dl:
                        self.current_page = self.page
                        return
                    # Try catching real popup with a fresh expect
                    try:
                        async with self.page.context.expect_page(timeout=15000) as rpi:
                            pass
                        rp = await rpi.value
                        await rp.wait_for_load_state("load", timeout=15000)
                        self.current_page = rp
                        return
                    except Exception:
                        self.current_page = self.page
                        return
                else:
                    self.current_page = popup
                    return
            except Exception:
                await asyncio.sleep(2)
                continue

        self.current_page = self.page

    def check_captcha(self) -> bool:
        return self.waiting_for_captcha

    async def handle_captcha_auto(self) -> bool:
        """Look for captcha, if found extract image b64, return True if captcha present."""
        try:
            captcha_div = await self.page.query_selector(
                'div.text-h6.text-center.q-mb-md:text("Security Check")'
            )
            if not captcha_div:
                return False
            img = await self.page.query_selector("img.q-img__image")
            if img:
                src = await img.get_attribute("src")
                if src and src.startswith("data:image"):
                    self.captcha_image_b64 = src
                    self.waiting_for_captcha = True
                    return True
        except Exception:
            pass
        return False

    async def submit_captcha(self, code: str) -> bool:
        try:
            inp = self.page.locator('input[placeholder*="Enter the code"]')
            await inp.fill(code)
            await asyncio.sleep(0.5)
            await self.page.locator('span.block:text("Confirm")').first.click()
            await asyncio.sleep(2)
            self.waiting_for_captcha = False
            self.captcha_image_b64 = ""
            return True
        except Exception:
            return False

    async def wait_for_dialog(self):
        current = getattr(self, "current_page", self.page)
        for _ in range(60):
            try:
                title_el = await current.query_selector(
                    'div.iframe-dialog-title, div[class*="dialog-title"]'
                )
                if title_el:
                    self.title_text = (await title_el.inner_text()).strip()
                    if self.title_text:
                        return
            except Exception:
                pass
            try:
                inner = await current.inner_text("body")
                if inner and "Copy Download Link" in inner:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)

    async def click_copy_link(self):
        current = getattr(self, "current_page", self.page)
        for attempt in range(40):
            try:
                btn = current.locator('button:has-text("Copy Download Link")').first
                await btn.click(timeout=2000)
                await asyncio.sleep(0.3)
                return True
            except Exception:
                pass
            try:
                btn = current.locator('span.block:has-text("Copy Download Link")').first
                await btn.click(timeout=2000)
                await asyncio.sleep(0.3)
                return True
            except Exception:
                pass
            try:
                btn = current.locator(
                    '[class*="q-btn"]:has-text("Copy Download Link")'
                ).first
                await btn.click(timeout=2000)
                await asyncio.sleep(0.3)
                return True
            except Exception:
                pass
            try:
                all_spans = await current.query_selector_all("span.block")
                for sp in all_spans:
                    txt = (await sp.inner_text()).strip()
                    if txt == "Copy Download Link":
                        box = await sp.bounding_box()
                        if box:
                            await current.mouse.click(
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                            return True
            except Exception:
                pass
            try:
                for fr in await current.query_selector_all("iframe"):
                    try:
                        fr_content = await fr.content_frame()
                        if fr_content:
                            btn = fr_content.locator(
                                'button:has-text("Copy Download Link"), span:has-text("Copy Download Link")'
                            ).first
                            await btn.click(timeout=1000)
                            await asyncio.sleep(0.3)
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    async def get_download_url(self) -> str:
        current = getattr(self, "current_page", self.page)
        for attempt in range(60):
            # 1) scan DOM for /get? URLs (converter URL — preferred)
            try:
                url = await current.evaluate("""() => {
                    const a = document.querySelector('a[href*="/get?"]');
                    if (a) return a.href;
                    const inp = document.querySelector('input[value*="/get?"]');
                    if (inp) return inp.value;
                    return null;
                }""")
                if url:
                    self.download_url = url
                    return url
            except Exception:
                pass

            # 2) scan ALL elements for /get? or sf-converter.com URLs
            try:
                url = await current.evaluate("""() => {
                    const els = document.querySelectorAll('*');
                    for (const el of els) {
                        const t = el.textContent.trim();
                        if (t.startsWith('http://') || t.startsWith('https://')) {
                            if (t.includes('/get?') || t.includes('sf-converter.com/get')) return t;
                        }
                    }
                    return null;
                }""")
                if url:
                    self.download_url = url
                    return url
            except Exception:
                pass

            # 3) regex on body inner_text for /get? URLs
            try:
                body = await current.inner_text("body")
                for match in re.finditer(r'https?://[^\s"\']+/get\?[^\s"\']+', body):
                    self.download_url = match.group(0)
                    return match.group(0)
            except Exception:
                pass

            # 4) clipboard (filtered — only converter URLs)
            try:
                text = await current.evaluate("""async () => {
                    try { const t = await navigator.clipboard.readText(); if (t && (t.includes('/get?') || t.includes('sf-converter.com/get'))) return t; } catch(e) {}
                    return '';
                }""")
                if text:
                    self.download_url = text
                    return text
            except Exception:
                pass

            # 5) clipboard via hidden input (filtered)
            try:
                url = await current.evaluate("""async () => {
                    try {
                        const inp = document.createElement('input');
                        inp.style.position = 'fixed'; inp.style.left = '-9999px';
                        document.body.appendChild(inp);
                        inp.focus();
                        const t = await navigator.clipboard.readText();
                        inp.value = t;
                        inp.remove();
                        if (t && (t.includes('/get?') || t.includes('sf-converter.com/get'))) return t;
                    } catch(e) {}
                    return '';
                }""")
                if url:
                    self.download_url = url
                    return url
            except Exception:
                pass

            # 6) catch-all: clipboard via execCommand (more reliable in headless)
            try:
                text = await current.evaluate("""() => {
                    try {
                        var inp = document.createElement('input');
                        inp.style.position = 'fixed'; inp.style.left = 0; inp.style.top = 0;
                        inp.style.width = '1px'; inp.style.height = '1px';
                        document.body.appendChild(inp);
                        inp.focus();
                        inp.select();
                        var ok = document.execCommand('paste');
                        var val = inp.value.trim();
                        inp.remove();
                        if (ok && val && val.startsWith('http')) return val;
                    } catch(e) {}
                    return '';
                }""")
                if text:
                    self.download_url = text
                    return text
            except Exception:
                pass

            # 7) catch-all: clipboard async API fallback
            try:
                text = await current.evaluate("""async () => {
                    try { const t = await navigator.clipboard.readText(); if (t && t.startsWith('http')) return t; } catch(e) {}
                    return '';
                }""")
                if text:
                    self.download_url = text
                    return text
            except Exception:
                pass

            await asyncio.sleep(0.5)
        return ""

    async def run_full_flow(self, video_url: str) -> dict:
        steps = []
        try:
            steps.append("Starting browser...")
            await self.start_browser()
            steps.append("Navigating to snapwc.com...")
            await self.navigate()
            steps.append("Pasting URL...")
            await self.paste_url(video_url)
            steps.append("Clicking Get Download Links...")
            await self.click_get_links()
            steps.append("Waiting for conversion...")
            await self.wait_for_conversion()
            steps.append("Parsing qualities...")
            await self.parse_qualities()
            steps.append(f"Found {len(self.qualities)} quality options")

            return {
                "success": True,
                "qualities": [
                    {
                        "index": i,
                        "label": q["label"],
                        "category": q["category"],
                        "size": q.get("size", ""),
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

    async def continue_with_quality(self, index: int) -> dict:
        steps = []
        try:
            steps.append(f"Selecting quality #{index}...")
            await self.select_and_click_download(index)
            steps.append("Checking for captcha...")

            captcha = await self.handle_captcha_auto()
            if captcha:
                steps.append("Captcha detected!")
                return {
                    "success": True,
                    "captcha": True,
                    "captcha_image": self.captcha_image_b64,
                    "steps": steps,
                    "session": self,
                }

            steps.append("Waiting for download dialog...")
            await self.wait_for_dialog()
            steps.append("Clicking Copy Download Link...")
            copied = await self.click_copy_link()
            if not copied:
                steps.append("Copy link button not found, checking captcha again...")
                captcha = await self.handle_captcha_auto()
                if captcha:
                    steps.append("Captcha detected on retry!")
                    return {
                        "success": True,
                        "captcha": True,
                        "captcha_image": self.captcha_image_b64,
                        "steps": steps,
                        "session": self,
                    }
                await self.take_screenshot()
                return {
                    "success": False,
                    "error": "Could not find Copy Download Link",
                    "steps": steps,
                    "screenshot_b64": self.screenshot_b64,
                    "session": self,
                }

            steps.append("Retrieving download URL...")
            url = await self.get_download_url()
            if not url:
                await self.take_screenshot()
                return {
                    "success": False,
                    "error": "Failed to get download URL",
                    "steps": steps,
                    "screenshot_b64": self.screenshot_b64,
                    "session": self,
                }

            steps.append("Got download link! Cleaning up...")
            await self.close_browser()
            return {
                "success": True,
                "captcha": False,
                "download_url": url,
                "title": self.title_text,
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

    async def continue_after_captcha(self, code: str, index: int) -> dict:
        steps = []
        try:
            steps.append("Submitting captcha code...")
            ok = await self.submit_captcha(code)
            if not ok:
                return {
                    "success": False,
                    "error": "Captcha submission failed",
                    "steps": steps,
                    "session": self,
                }

            steps.append("Captcha submitted, re-selecting quality...")
            await asyncio.sleep(2)
            await self.select_and_click_download(index)

            steps.append("Waiting for download dialog...")
            await self.wait_for_dialog()
            steps.append("Clicking Copy Download Link...")
            copied = await self.click_copy_link()
            if not copied:
                await self.take_screenshot()
                return {
                    "success": False,
                    "error": "Could not find Copy Download Link after captcha",
                    "steps": steps,
                    "screenshot_b64": self.screenshot_b64,
                    "session": self,
                }

            steps.append("Retrieving download URL...")
            url = await self.get_download_url()
            if not url:
                await self.take_screenshot()
                return {
                    "success": False,
                    "error": "Failed to get download URL after captcha",
                    "steps": steps,
                    "screenshot_b64": self.screenshot_b64,
                    "session": self,
                }

            steps.append("Got download link! Cleaning up...")
            await self.close_browser()
            return {
                "success": True,
                "captcha": False,
                "download_url": url,
                "title": self.title_text,
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


# ─── Standalone CLI usage ──────────────────────────────────────────
async def main():
    print("SnapWC Video Downloader")
    video_url = input("Paste video URL: ").strip()
    session = SnapWCSession()
    result = await session.run_full_flow(video_url)

    if not result["success"]:
        print(f"Error: {result['error']}")
        return

    print("\nAvailable qualities:")
    for q in result["qualities"]:
        sz = f" ({q['size']})" if q.get("size") else ""
        print(f"  {q['index'] + 1}: [{q['category']}] {q['label']}{sz}")

    choice = int(input("Select quality: ")) - 1
    result2 = await session.continue_with_quality(choice)

    if result2.get("captcha"):
        print("Captcha detected! Check the browser window.")
        code = input("Enter captcha code: ")
        result2 = await session.continue_after_captcha(code, choice)

    if result2["success"] and result2.get("download_url"):
        print(f"\nDownload URL: {result2['download_url']}")
        print(f"Title: {result2.get('title', '')}")
    else:
        print(f"Error: {result2.get('error', 'Unknown')}")


if __name__ == "__main__":
    asyncio.run(main())
