import asyncio
import re
import aiohttp


async def extract_youtube_info(url: str) -> str:
    # Try YouTube oEmbed API first (fast, no browser needed)
    try:
        oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(oembed_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    title = (data.get("title", "") or "").strip()
                    if title:
                        return title
    except Exception:
        pass

    # Fallback: scrape YouTube page title directly
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
                    if m:
                        title = m.group(1).replace(" - YouTube", "").strip()
                        if title:
                            return title
    except Exception:
        pass

    return ""


async def main():
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else input("Enter YouTube URL: ").strip()
    if not url:
        print("No URL provided.")
        return
    result = await extract_youtube_info(url)
    print(f"Title: {result}" if result else "No title extracted.")


if __name__ == "__main__":
    asyncio.run(main())
