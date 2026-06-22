import asyncio
import re
import json
import aiohttp


async def extract_youtube_info(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    html = ""
    timeout = aiohttp.ClientTimeout(total=15)

    # Fetch YouTube page HTML
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    html = await resp.text()
    except Exception:
        pass

    title = ""
    description = ""

    if html:
        # Try JSON-LD first (most reliable)
        try:
            for m in re.finditer(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                html,
                re.DOTALL,
            ):
                data = json.loads(m.group(1))
                if isinstance(data, list):
                    data = data[0]
                if data.get("@type") == "VideoObject" or data.get("name"):
                    if not title:
                        title = (data.get("name", "") or "").strip()
                    if not description:
                        description = (data.get("description", "") or "").strip()
                    break
        except Exception:
            pass

        # Fallback: meta tags
        if not title:
            m = re.search(
                r'<meta\s+name="title"\s+content="([^"]*)"', html, re.IGNORECASE
            )
            if m:
                title = m.group(1).strip()
        if not description:
            m = re.search(
                r'<meta\s+name="description"\s+content="([^"]*)"',
                html,
                re.IGNORECASE,
            )
            if m:
                description = m.group(1).strip()

        # Last resort: <title> tag
        if not title:
            m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
            if m:
                title = m.group(1).replace(" - YouTube", "").strip()

    # If page scraping failed entirely, use oEmbed for title only
    if not title:
        try:
            oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(oembed_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        title = (data.get("title", "") or "").strip()
        except Exception:
            pass

    # Clean up title
    title = re.sub(r"\s+", " ", title).strip() if title else ""
    description = re.sub(r"\s+", " ", description).strip() if description else ""
    # Truncate very long descriptions
    if len(description) > 500:
        description = description[:497] + "..."

    if title and description:
        return f"{title}\n{description}"
    if title:
        return title
    return ""


async def main():
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else input("Enter YouTube URL: ").strip()
    if not url:
        print("No URL provided.")
        return
    result = await extract_youtube_info(url)
    if result:
        lines = result.split("\n")
        print(f"Title: {lines[0]}")
        if len(lines) > 1:
            print(f"Description: {lines[1]}")
    else:
        print("No info extracted.")


if __name__ == "__main__":
    asyncio.run(main())
