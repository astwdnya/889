import asyncio
import re
import os
import sys
import time
import random


from playwright.async_api import async_playwright
import aiohttp
import aiofiles


URL_PATTERN = re.compile(r"https?://[^\s/$.?#].[^\s]*", re.IGNORECASE)
PROGRESS_BAR_LENGTH = 40

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"
CLEAR = "\033[2K\r"


# ─── Stealth: JS patches to hide automation ─────────────────────────────
STEALTH_JS = """
// Override webdriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// Override plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' })),
});

// Override languages
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// Override chrome runtime
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
};

// Override permissions
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);

// Override webdriver detection in WebGL
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter(parameter);
};

// Override canvas fingerprint (subtle)
Object.defineProperty(HTMLCanvasElement.prototype, 'toBlob', {
    value: function(callback, type, quality) {
        setTimeout(callback.bind(this, null), 0);
    }
});
"""


def sizeof_fmt(num: float, suffix: str = "B") -> str:
    for unit in ("", "K", "M", "G", "T"):
        if abs(num) < 1024.0:
            return f"{num:>7.2f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:>7.2f} P{suffix}"


def eta_fmt(seconds: float) -> str:
    if seconds < 0:
        return "∞"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


def progress_bar(percent: float) -> str:
    filled = int(PROGRESS_BAR_LENGTH * percent / 100)
    return "█" * filled + "░" * (PROGRESS_BAR_LENGTH - filled)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "video"
    if len(name) > 200:
        name = name[:200]
    return name


# ─── Human-like behavior helpers ───────────────────────────────────────
async def human_type(page, selector: str, text: str):
    el = page.locator(selector)
    await el.click()
    await el.fill("")
    await asyncio.sleep(random.uniform(0.1, 0.3))
    for char in text:
        await el.type(char, delay=random.randint(40, 120))
        if random.random() < 0.05:
            await asyncio.sleep(random.uniform(0.1, 0.4))


async def human_click(page, selector_or_el, timeout=10000):
    if isinstance(selector_or_el, str):
        el = page.locator(selector_or_el).first
        await el.wait_for(timeout=timeout)
    else:
        el = selector_or_el

    box = await el.bounding_box()
    if box:
        x = box["x"] + box["width"] * random.uniform(0.2, 0.8)
        y = box["y"] + box["height"] * random.uniform(0.2, 0.8)
        await page.mouse.move(
            box["x"] + box["width"] * 0.1,
            box["y"] + box["height"] * 0.1,
            steps=random.randint(5, 12),
        )
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.mouse.move(x, y, steps=random.randint(8, 20))
        await asyncio.sleep(random.uniform(0.05, 0.2))
        await page.mouse.click(x, y)
    else:
        await el.click()


async def random_scroll(page):
    for _ in range(random.randint(1, 3)):
        delta = random.randint(100, 400) * random.choice([-1, 1])
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(random.uniform(0.3, 1.0))


# ─── Get download URL ──────────────────────────────────────────────────
async def get_download_url(page) -> str | None:
    for attempt in range(60):
        # method 1: clipboard
        try:
            text = await page.evaluate(
                """async () => {
                    try {
                        const t = await navigator.clipboard.readText();
                        if (t && t.startsWith('http')) return t;
                    } catch(e) {}
                    return '';
                }"""
            )
            if text and text.startswith("http"):
                return text
        except Exception:
            pass

        # method 2: look for <a> or <input> with /get? in href/value
        try:
            url = await page.evaluate(
                """() => {
                    const a = document.querySelector('a[href*="/get?"]');
                    if (a) return a.href;
                    const inp = document.querySelector('input[value*="/get?"]');
                    if (inp) return inp.value;
                    return null;
                }"""
            )
            if url:
                return url
        except Exception:
            pass

        # method 3: scan every element's text for an http URL
        try:
            url = await page.evaluate(
                """() => {
                    const els = document.querySelectorAll('*');
                    for (const el of els) {
                        const t = el.textContent.trim();
                        if (t.startsWith('http://') || t.startsWith('https://')) {
                            if (t.includes('/get?') || t.includes('sf-converter.com/get')) return t;
                        }
                    }
                    return null;
                }"""
            )
            if url:
                return url
        except Exception:
            pass

        # method 4: get full page text and regex search
        try:
            body = await page.inner_text("body")
            for match in re.finditer(r'https?://[^\s"\']+/get\?[^\s"\']+', body):
                return match.group(0)
        except Exception:
            pass

        # method 5: tap Ctrl+V onto a hidden input and read it
        try:
            url = await page.evaluate(
                """async () => {
                    try {
                        const inp = document.createElement('input');
                        inp.style.position = 'fixed';
                        inp.style.left = '-9999px';
                        document.body.appendChild(inp);
                        inp.focus();
                        await navigator.clipboard.readText().then(t => { inp.value = t; });
                        const val = inp.value;
                        inp.remove();
                        if (val.startsWith('http')) return val;
                    } catch(e) {}
                    return '';
                }"""
            )
            if url and url.startswith("http"):
                return url
        except Exception:
            pass

        await asyncio.sleep(0.5)
    return None


# ─── Download with live progress ───────────────────────────────────────
async def download_with_progress(url: str, filename: str) -> bool:
    connector = aiohttp.TCPConnector(limit_per_host=10, ssl=False)
    timeout = aiohttp.ClientTimeout(total=None)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://snapwc.com/",
    }

    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout
        ) as session:
            async with session.get(
                url, headers=headers, allow_redirects=True, ssl=False
            ) as resp:
                if resp.status not in (200, 206):
                    print(f"  {RED}Download failed — HTTP {resp.status}{RESET}")
                    return False

                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                start_time = time.time()
                last_update = 0.0

                async with aiofiles.open(filename, "wb") as f:
                    async for chunk in resp.content.iter_chunked(128 * 1024):
                        await f.write(chunk)
                        downloaded += len(chunk)

                        now = time.time()
                        if now - last_update < 0.12:
                            continue
                        last_update = now

                        elapsed = now - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        percent = (downloaded / total * 100) if total > 0 else 0
                        eta = (total - downloaded) / speed if speed > 0 else 0

                        bar = progress_bar(percent)
                        sys.stdout.write(
                            f"{CLEAR}  {bar}  "
                            f"{GREEN}{percent:>5.1f}%{RESET}  "
                            f"{sizeof_fmt(downloaded)} / {sizeof_fmt(total)}  "
                            f"{CYAN}{sizeof_fmt(speed)}/s{RESET}  "
                            f"ETA {YELLOW}{eta_fmt(eta)}{RESET}"
                        )
                        sys.stdout.flush()

                elapsed = time.time() - start_time
                avg_speed = downloaded / elapsed if elapsed > 0 else 0
                print(
                    f"\n  {GREEN}✔ Downloaded{RESET}  "
                    f"{sizeof_fmt(downloaded)} in {eta_fmt(elapsed)}  "
                    f"({CYAN}{sizeof_fmt(avg_speed)}/s avg{RESET})"
                )
                return True
    except Exception as e:
        print(f"  {RED}aiohttp error: {e}{RESET}")
        return False


# ─── Main ──────────────────────────────────────────────────────────────
async def main():
    print(f"{BOLD}{CYAN}╔══════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║       SnapWC Video Downloader Bot        ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════╝{RESET}\n")

    video_url = input(f"  {BOLD}Paste video URL:{RESET} ").strip()
    while not URL_PATTERN.match(video_url):
        print(f"  {RED}Invalid URL.{RESET}")
        video_url = input(f"  {BOLD}Paste video URL:{RESET} ").strip()

    print(f"  {YELLOW}Launching browser ...{RESET}")

    user_agent = random.choice(
        [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
        ]
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-infobars",
                "--disable-popup-blocking",
                "--disable-notifications",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-field-trial-config",
                "--disable-breakpad",
                "--disable-crash-reporter",
                "--no-crash-keys",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-pings",
                "--mute-audio",
                "--disable-quic",
                "--window-size=1280,800",
                "--lang=en-US",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1.0,
            is_mobile=False,
            has_touch=False,
            user_agent=user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            geolocation={"latitude": 40.7128, "longitude": -74.0060},
            permissions=["geolocation", "clipboard-read", "clipboard-write"],
            color_scheme="light",
            reduced_motion="no-preference",
            forced_colors="none",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Ch-Ua": '"Not/A)Brand";v="99", "Google Chrome";v="125", "Chromium";v="125"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
            },
        )

        try:
            await context.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception:
            pass

        page = await context.new_page()

        # ─── Inject stealth JS before any navigation ────────────────
        await page.add_init_script(STEALTH_JS)

        print(f"  {YELLOW}Navigating to snapwc.com ...{RESET}")
        for retry in range(3):
            try:
                await page.goto(
                    "https://snapwc.com/sites", wait_until="load", timeout=60000
                )
                break
            except Exception as e:
                if retry < 2:
                    print(f"  {YELLOW}Navigation failed ({e}), retrying ...{RESET}")
                    await asyncio.sleep(3)
                else:
                    raise
        await asyncio.sleep(random.uniform(1.0, 2.0))

        # ─── Scroll randomly like a human ───────────────────────────
        await random_scroll(page)

        # ─── Human-like URL paste ───────────────────────────────────
        input_selector = 'input[name="video-url-input"]'
        await page.wait_for_selector(input_selector, timeout=15000)
        await human_type(page, input_selector, video_url)
        print(f"  {GREEN}✔ URL pasted{RESET}")

        await asyncio.sleep(random.uniform(0.5, 1.0))

        # ─── Click "Get Download Links" ─────────────────────────────
        await human_click(page, 'span.block:text("Get Download Links")')
        print(f"  {GREEN}✔ Get Download Links clicked{RESET}")

        # ─── Wait for conversion to complete ────────────────────────
        print(f"  {YELLOW}Converting video ...{RESET}")
        progress_selector = "div.text-center.text-caption.text-grey"
        last_progress = ""
        conversion_done = False

        while not conversion_done:
            try:
                el = await page.query_selector(progress_selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and text != last_progress:
                        print(f"  Progress: {text}")
                        last_progress = text
                    if text.rstrip("%") == "100":
                        print(f"  {GREEN}✔ Conversion complete{RESET}")
                        conversion_done = True
                # check if quality options appeared (conversion finished)
                test_q = await page.query_selector_all("div.q-item__label")
                for q_el in test_q:
                    t = (await q_el.inner_text()).strip()
                    if t and (re.search(r"\d{3,4}p", t) or "mp4" in t.lower()):
                        if not conversion_done:
                            print(
                                f"  {GREEN}✔ Conversion complete (qualities detected){RESET}"
                            )
                        conversion_done = True
                        break
            except Exception:
                pass
            await asyncio.sleep(1)

        await asyncio.sleep(random.uniform(1.0, 2.0))

        # ─── Parse quality options ──────────────────────────────
        qualities = []

        for _ in range(120):
            all_labels = await page.query_selector_all("div.q-item__label")
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

                # determine category by walking up/back to section header
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

                # find size caption next to this label
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

                found.append({"label": t, "el": el, "category": cat, "size": size})

            if found:
                qualities = found
                break
            await asyncio.sleep(1)
        else:
            print(f"  {RED}No quality options found!{RESET}")
            await browser.close()
            return

        # ─── Show quality menu grouped by category ──────────────
        print(f"\n  {BOLD}Available qualities:{RESET}")
        cats = ["Video", "No Sound", "Audio"]
        idx = 1
        for cat in cats:
            items = [q for q in qualities if q["category"] == cat]
            if not items:
                continue
            cat_icon = {"Video": "🎬", "No Sound": "🔇", "Audio": "🎵"}[cat]
            print(f"  {BOLD}{cat_icon} {cat}{RESET}")
            for q in items:
                size_str = f" ({q['size']})" if q.get("size") else ""
                print(f"    {CYAN}{idx}{RESET}: {q['label']}{size_str}")
                q["menu_idx"] = idx
                idx += 1
            print()

        choice = input(f"  {BOLD}Select quality (1-{len(qualities)}):{RESET} ").strip()
        while not choice.isdigit() or not (1 <= int(choice) <= len(qualities)):
            print(f"  {RED}Invalid choice.{RESET}")
            choice = input(
                f"  {BOLD}Select quality (1-{len(qualities)}):{RESET} "
            ).strip()

        selected = next(q for q in qualities if q["menu_idx"] == int(choice))
        quality_label = selected["label"]

        print(f"  {GREEN}✔ Selected: {quality_label} ({selected['category']}){RESET}")

        # ─── Find and click download icon ───────────────────────────
        target_el = selected["el"]

        async with page.context.expect_page(timeout=10000) as popup_info:
            # find the file_download icon near this quality item
            dl_handle = await target_el.evaluate_handle("""el => {
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
                await human_click(page, dl_elem)
            else:
                btns = await page.query_selector_all(
                    'button, [class*="q-btn"], [role="button"]'
                )
                clicked = False
                for btn in btns:
                    try:
                        if await btn.is_visible():
                            await human_click(page, btn)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    await human_click(page, target_el)

        print(f"  {GREEN}✔ Download action triggered{RESET}")

        # ─── Wait for popup/dialog ──────────────────────────────────
        popup = None
        popup_is_fake = False
        try:
            popup = await popup_info.value
            await popup.wait_for_load_state("load", timeout=15000)
            popup_url = popup.url
            if "offer-support" in popup_url or "supportsnapwc" in popup_url:
                print(f"  {YELLOW}Fake support popup detected, closing ...{RESET}")
                await popup.close()
                popup_is_fake = True
                current_page = page
            else:
                print(f"  {GREEN}✔ Download popup opened{RESET}")
                current_page = popup
        except Exception:
            print(f"  {YELLOW}No popup detected, using current page{RESET}")
            current_page = page

        await asyncio.sleep(2)

        # ─── Wait for download dialog ───────────────────────────
        print(f"  {YELLOW}Waiting for download dialog ...{RESET}")
        title_text = ""
        dialog_found = False

        for _ in range(60):
            # check for title
            try:
                title_el = await current_page.query_selector(
                    'div.iframe-dialog-title, div[class*="dialog-title"]'
                )
                if title_el:
                    title_text = (await title_el.inner_text()).strip()
                    if title_text:
                        print(f"  {GREEN}✔ Dialog opened — {title_text}{RESET}")
                        dialog_found = True
            except Exception:
                pass

            # check for Copy Download Link button already there
            if not dialog_found:
                try:
                    inner = await current_page.inner_text("body")
                    if inner and "Copy Download Link" in inner:
                        print(f"  {GREEN}✔ Download page detected{RESET}")
                        dialog_found = True
                except Exception:
                    pass

            if dialog_found:
                break
            await asyncio.sleep(0.5)

        await asyncio.sleep(random.uniform(2.0, 3.5))

        # ─── Click "Copy Download Link" ─────────────────────────
        print(f"  {YELLOW}Looking for Copy Download Link button ...{RESET}")
        copied = False

        for attempt in range(40):
            try:
                btn = current_page.locator(
                    'button:has-text("Copy Download Link")'
                ).first
                await btn.click(timeout=2000)
                await asyncio.sleep(0.3)
                copied = True
                break
            except Exception:
                pass

            try:
                btn = current_page.locator(
                    'span.block:has-text("Copy Download Link")'
                ).first
                await btn.click(timeout=2000)
                await asyncio.sleep(0.3)
                copied = True
                break
            except Exception:
                pass

            try:
                btn = current_page.locator(
                    '[class*="q-btn"]:has-text("Copy Download Link")'
                ).first
                await btn.click(timeout=2000)
                await asyncio.sleep(0.3)
                copied = True
                break
            except Exception:
                pass

            # last resort in each loop: search all elements by text
            try:
                all_spans = await current_page.query_selector_all("span.block")
                for sp in all_spans:
                    txt = (await sp.inner_text()).strip()
                    if txt == "Copy Download Link":
                        box = await sp.bounding_box()
                        if box:
                            await current_page.mouse.click(
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                            copied = True
                            break
                if copied:
                    break
            except Exception:
                pass

            # last resort: search inside iframes too
            try:
                for fr in await current_page.query_selector_all("iframe"):
                    try:
                        fr_content = await fr.content_frame()
                        if fr_content:
                            btn = fr_content.locator(
                                'button:has-text("Copy Download Link"), span:has-text("Copy Download Link")'
                            ).first
                            await btn.click(timeout=1000)
                            copied = True
                            await asyncio.sleep(0.3)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            await asyncio.sleep(0.5)

        if copied:
            print(f"  {GREEN}✔ Copy Download Link clicked{RESET}")
        else:
            print(f"  {RED}Could not find Copy Download Link button.{RESET}")
            await browser.close()
            return

        # ─── Retrieve download URL ──────────────────────────────────
        print(f"  {YELLOW}Retrieving download link ...{RESET}")
        await asyncio.sleep(1)

        download_url = await get_download_url(current_page)

        if not download_url:
            print(f"  {RED}Failed to get download URL.{RESET}")
            await browser.close()
            return

        print(f"  {GREEN}✔ Download URL obtained{RESET}")
        print(f"  URL: {download_url[:90]}...")

        # ─── Download ───────────────────────────────────────────
        if not title_text:
            title_text = quality_label

        filename = sanitize_filename(title_text)
        filename = f"downloads/{filename}.mp4"
        os.makedirs("downloads", exist_ok=True)

        print(f"\n  {BOLD}Downloading to:{RESET} {filename}")

        # Try direct aiohttp download; fallback to browser download monitor
        dl_success = await download_with_progress(download_url, filename)

        if dl_success:
            print(f"\n  {GREEN}{BOLD}✔ Done! Saved as: {filename}{RESET}")
        else:
            print(f"  {YELLOW}aiohttp failed, monitoring browser download ...{RESET}")

            # monitor browser's download progress caption
            print(f"  {YELLOW}Waiting for browser download to complete ...{RESET}")
            try:
                while True:
                    cap = await page.query_selector("span.progress-caption")
                    if cap:
                        txt = (await cap.inner_text()).strip()
                        print(f"  Browser: {txt}")
                        if "Downloaded" in txt:
                            match = re.search(r"([\d.]+)\s*MB\s*/\s*([\d.]+)\s*MB", txt)
                            if match:
                                cur = float(match.group(1))
                                total = float(match.group(2))
                                pct = cur / total * 100 if total > 0 else 0
                                bar = progress_bar(pct)
                                sys.stdout.write(
                                    f"{CLEAR}  {bar}  {GREEN}{pct:>5.1f}%{RESET}  "
                                    f"{match.group(1)}MB / {match.group(2)}MB"
                                )
                                sys.stdout.flush()
                                if cur >= total:
                                    print(
                                        f"\n  {GREEN}✔ Browser download complete{RESET}"
                                    )
                                    dl_success = True
                                    break
                    await asyncio.sleep(1)
            except Exception:
                pass

        await browser.close()
        if dl_success:
            print(f"\n  {GREEN}{BOLD}✔ Done! Saved as: {filename}{RESET}")
        else:
            print(f"\n  {RED}Download failed.{RESET}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n  {YELLOW}Bot stopped by user{RESET}")
    except asyncio.CancelledError:
        print(f"\n  {YELLOW}Bot cancelled{RESET}")
