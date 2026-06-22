import asyncio
import sys
import random
import re
from playwright.async_api import async_playwright


async def extract_youtube_info(url: str) -> str:
    user_agent = random.choice(
        [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        ]
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
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

        url_input = page.locator("#input")
        await url_input.wait_for(timeout=15000)
        await url_input.click()
        await url_input.fill("")
        await url_input.type(url, delay=30)

        extract_btn = page.locator(
            'span[wire\\:target="onYoutubeDescriptionExtractor"]'
        )
        await extract_btn.wait_for(timeout=10000)
        await extract_btn.click()

        textarea = page.locator("#text")
        await textarea.wait_for(timeout=20000)
        await asyncio.sleep(2)

        content = await textarea.input_value()

        await browser.close()

    return content.strip()


async def main():
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("Enter YouTube URL: ").strip()

    if not url:
        print("No URL provided.")
        return

    content = await extract_youtube_info(url)
    print("\n" + "=" * 60)
    print("TITLE & DESCRIPTION:")
    print("=" * 60)
    print(content)
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
