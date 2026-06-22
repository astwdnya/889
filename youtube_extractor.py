import asyncio
import re
from playwright.async_api import async_playwright


def _unescape_json(s: str) -> str:
    return (
        s.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def _extract_video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/v/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})", url)
    return m.group(1) if m else ""


def _clean_title(title: str) -> str:
    title = title.strip()
    if title.endswith("(2)"):
        title = title[:-3].strip()
    return title


async def extract_youtube_info(url: str) -> dict:
    video_id = _extract_video_id(url)
    title = ""
    description = ""
    thumb_url = ""

    async with async_playwright() as playwright:
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

            result = await page.evaluate("""() => {
                const snippet = document.querySelector('#video #snippet code');
                if (!snippet) return '';

                const attrs = snippet.querySelectorAll('span.hljs-attr');
                let t = '', d = '';

                for (const attr of attrs) {
                    const key = attr.textContent.trim().replace(/^"(.*)"$/, '$1');
                    if (key === 'title' && !t) {
                        let el = attr.nextElementSibling;
                        while (el) {
                            if (el.classList.contains('hljs-string')) {
                                t = el.textContent.trim();
                                t = t.replace(/^"(.*)"$/, '$1');
                                break;
                            }
                            el = el.nextElementSibling;
                        }
                    } else if (key === 'description' && !d) {
                        let el = attr.nextElementSibling;
                        while (el) {
                            if (el.classList.contains('hljs-string')) {
                                d = el.textContent.trim();
                                d = d.replace(/^"(.*)"$/, '$1');
                                break;
                            }
                            el = el.nextElementSibling;
                        }
                    }
                    if (t && d) break;
                }

                if (t || d) return (t || '') + '\\n' + (d || '');
                return '';
            }""")

            if result:
                parts = result.split("\n", 1)
                title = _unescape_json(parts[0].strip())
                description = _unescape_json(parts[1].strip()) if len(parts) > 1 else ""

            # Extract thumbnail from page
            thumb_url = await page.evaluate(
                """() => {
                    const img = document.querySelector('#video-thumb');
                    return img ? img.src : '';
                }"""
            )

            await browser.close()
        except Exception:
            await browser.close()
            raise

    title = _clean_title(title)
    if not thumb_url and video_id:
        thumb_url = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"

    return {
        "title": title,
        "description": description,
        "video_id": video_id,
        "thumb_url": thumb_url,
    }


if __name__ == "__main__":
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else input("Enter YouTube URL: ").strip()
    if url:
        result = asyncio.run(extract_youtube_info(url))
        print(f"Title: {result.get('title', '')}")
        print(f"Description: {result.get('description', '')}")
        print(f"Video ID: {result.get('video_id', '')}")
        print(f"Thumb URL: {result.get('thumb_url', '')}")
