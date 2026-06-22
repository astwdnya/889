import asyncio
from playwright.async_api import async_playwright


async def extract_youtube_info(url: str) -> str:
    playwright = await async_playwright().__aenter__()
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
        ],
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    )
    page = await context.new_page()

    try:
        await page.goto(
            "https://mattw.io/youtube-metadata/",
            wait_until="domcontentloaded",
            timeout=30000,
        )

        await page.locator("#value").wait_for(timeout=10000)
        await page.locator("#value").click()
        await page.locator("#value").fill("")
        await asyncio.sleep(0.3)
        await page.locator("#value").fill(url)
        await asyncio.sleep(0.3)

        await page.locator('span:has-text("Submit")').first.click()

        await page.wait_for_timeout(3000)

        title = ""
        description = ""

        spans = await page.locator("span.hljs-string").all()
        for i, sp in enumerate(spans):
            text = (await sp.inner_text()).strip().strip('"')
            if not text:
                continue
            if not title:
                title = text
            elif not description:
                description = text
                break

        return f"{title}\n{description}" if title else ""
    finally:
        await browser.close()
        await playwright.__aexit__(None, None, None)


if __name__ == "__main__":
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else input("Enter YouTube URL: ").strip()
    if url:
        result = asyncio.run(extract_youtube_info(url))
        if result:
            lines = result.split("\n")
            print(f"Title: {lines[0]}")
            if len(lines) > 1:
                print(f"Description: {lines[1]}")
        else:
            print("No info extracted.")
