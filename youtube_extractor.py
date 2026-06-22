import asyncio
import sys
import random
import re
import base64
from playwright.async_api import async_playwright, TimeoutError as PwTimeout


async def extract_youtube_info(url: str) -> str:
    user_agent = random.choice(
        [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        ]
    )

    last_error = ""
    screenshot_b64 = ""

    for attempt in range(3):
        playwright = await async_playwright().__aenter__()
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-web-security",
                "--disable-infobars",
                "--no-first-run",
                "--no-default-browser-check",
                "--window-size=1280,800",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=user_agent,
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await context.new_page()

        try:
            for retry in range(3):
                try:
                    await page.goto(
                        "https://seostudio.tools/youtube-description-extractor",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    break
                except Exception:
                    if retry < 2:
                        await asyncio.sleep(3)
                    else:
                        raise

            await asyncio.sleep(1)

            try:
                await page.locator("#input").wait_for(timeout=20000)
                await page.locator("#input").click(force=True)
                await asyncio.sleep(1)
                await page.locator("#input").fill(url)
            except Exception:
                try:
                    await page.evaluate(
                        f'document.querySelector("#input").value = "{url}"'
                    )
                    await page.evaluate(
                        'document.querySelector("#input").dispatchEvent(new Event("input"))'
                    )
                except Exception:
                    pass

            extract_btn = page.locator(
                'span[wire\\:target="onYoutubeDescriptionExtractor"]'
            )
            try:
                await extract_btn.wait_for(timeout=15000)
                await extract_btn.click(force=True)
            except Exception:
                try:
                    await page.evaluate(
                        'document.querySelector("button[type=submit]")?.click()'
                    )
                except Exception:
                    pass

            try:
                await page.locator("#text").wait_for(timeout=30000)
            except Exception:
                pass
            await asyncio.sleep(3)

            content = ""
            try:
                content = await page.locator("#text").input_value(timeout=10000)
            except Exception:
                try:
                    content = await page.evaluate(
                        'document.querySelector("#text")?.value || ""'
                    )
                except Exception:
                    pass

            if content and len(content) > 20:
                await browser.close()
                await playwright.__aexit__(None, None, None)
                return content.strip()

            try:
                ss = await page.screenshot(type="png")
                screenshot_b64 = base64.b64encode(ss).decode()
            except Exception:
                pass
            last_error = f"Attempt {attempt + 1}: empty content"
            await browser.close()
            await playwright.__aexit__(None, None, None)
            await asyncio.sleep(2)
        except Exception as e:
            try:
                ss = await page.screenshot(type="png")
                screenshot_b64 = base64.b64encode(ss).decode()
            except Exception:
                pass
            last_error = f"Attempt {attempt + 1}: {str(e)}"
            await browser.close()
            await playwright.__aexit__(None, None, None)

    # All attempts failed
    raise ExtractorError(last_error, screenshot_b64)


class ExtractorError(Exception):
    def __init__(self, msg, screenshot_b64=""):
        super().__init__(msg)
        self.screenshot_b64 = screenshot_b64


async def main():
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("Enter YouTube URL: ").strip()

    if not url:
        print("No URL provided.")
        return

    try:
        content = await extract_youtube_info(url)
        print("\n" + "=" * 60)
        print("TITLE & DESCRIPTION:")
        print("=" * 60)
        print(content)
        print("=" * 60)
    except ExtractorError as e:
        print(f"\nError: {e}")
        if e.screenshot_b64:
            print(f"Screenshot: data:image/png;base64,{e.screenshot_b64[:100]}...")


if __name__ == "__main__":
    asyncio.run(main())
