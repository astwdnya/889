"""
xvideos_search.py
─────────────────
سرچ ویدیو از XVideos با scraping.

استفاده:
  from xvideos_search import search_xvideos
  results = await search_xvideos("query", page=1, sort="relevance")

Inline trigger: xv:query
"""

import asyncio
import logging
import re
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import quote_plus

import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("XvideosSearch")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

_BASE_URL = "https://www.xvideos.com"

# مپ sort
_SORT_MAP = {
    "": "",
    "relevance": "",
    "uploaddate": "uploaddate",
    "date": "uploaddate",
    "new": "uploaddate",
    "mr": "uploaddate",
    "rating": "rating",
    "tr": "rating",
    "length": "length",
    "lg": "length",
    "views": "views",
    "mv": "views",
}


# ─── Data model ─────────────────────────────────────────────


@dataclass
class XvideosVideo:
    title: str
    url: str
    thumbnail: str
    preview_video: str
    duration: str
    views: str
    quality: str
    video_id: str
    eid: str
    channel: str
    source: str = "xvideos"

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Search ─────────────────────────────────────────────────


async def search_xvideos(
    query: str,
    page: int = 1,
    limit: int = 20,
    sort: str = "",
    hd: bool = False,
) -> List[dict]:
    """
    سرچ ویدیو از XVideos.

    Args:
        query: عبارت جستجو
        page: شماره صفحه (از 1)
        limit: حداکثر نتایج
        sort: مرتب‌سازی
            "" / "relevance" = مرتبط‌ترین
            "uploaddate" / "mr" / "new" = جدیدترین
            "rating" / "tr" = بهترین امتیاز
            "length" / "lg" = طولانی‌ترین
            "views" / "mv" = بیشترین بازدید
        hd: فقط HD

    Returns:
        لیست dict
    """
    if not query or len(query.strip()) < 2:
        return []

    query = query.strip()
    encoded = quote_plus(query)

    search_url = f"{_BASE_URL}/?k={encoded}"

    # Sort
    sort_val = _SORT_MAP.get(sort.lower(), "")
    if sort_val:
        search_url += f"&sort={sort_val}"

    # HD filter
    if hd:
        search_url += "&quality=hd"

    # Pagination (xvideos: page 1 = no param, page 2 = p=1)
    if page > 1:
        search_url += f"&p={page - 1}"

    logger.info(
        "XVideos search: q='%s' page=%d sort='%s' hd=%s",
        query, page, sort, hd,
    )

    html = await _fetch_page(search_url)
    if not html:
        return []

    results = _parse_search_results(html)

    if limit and len(results) > limit:
        results = results[:limit]

    logger.info("Found %d results for '%s'", len(results), query)
    return [r.to_dict() for r in results]


# ─── HTTP ───────────────────────────────────────────────────


async def _fetch_page(url: str) -> Optional[str]:
    """دریافت صفحه HTML."""
    # اول curl_cffi
    html = await _fetch_curl_cffi(url)
    if html:
        return html

    # fallback aiohttp
    timeout = ClientTimeout(total=20, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers=_HEADERS, allow_redirects=True
            ) as resp:
                if resp.status == 200:
                    return await resp.text(errors="replace")
                logger.warning("HTTP %d for %s", resp.status, url)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("aiohttp fetch failed: %s", e)

    return None


async def _fetch_curl_cffi(url: str) -> Optional[str]:
    """دریافت با curl_cffi."""
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return None

    try:
        async with AsyncSession() as session:
            resp = await session.get(
                url,
                impersonate="chrome",
                timeout=20,
            )
            if resp.status_code == 200:
                return resp.text
    except Exception as e:
        logger.debug("curl_cffi failed: %s", e)

    return None


# ─── HTML Parsing ───────────────────────────────────────────


def _parse_search_results(html: str) -> List[XvideosVideo]:
    """پارس نتایج سرچ XVideos."""
    results = []
    seen_ids = set()

    # پیدا کردن همه بلاک‌های ویدیو
    # هر ویدیو: <div id="video_{eid}" data-id="{id}" ...>
    # ادامه تا بلاک بعدی یا پایان mozaique
    block_pattern = re.compile(
        r'<div\s+id="video_([^"]+)"\s+data-id="(\d+)"[^>]*>(.*?)(?=<div\s+id="video_|<nav\b|<div\s+class="clear\b|$)',
        re.DOTALL | re.IGNORECASE,
    )

    for m in block_pattern.finditer(html):
        eid = m.group(1)
        vid_id = m.group(2)
        block = m.group(3)

        if vid_id in seen_ids:
            continue
        seen_ids.add(vid_id)

        video = _extract_video(eid, vid_id, block)
        if video:
            results.append(video)

    return results


def _extract_video(eid: str, vid_id: str, block: str) -> Optional[XvideosVideo]:
    """استخراج اطلاعات یک ویدیو از بلاک HTML."""

    # ─── Title ──────────────────────────────────────
    title = ""
    # p.title > a[title]
    title_m = re.search(
        r'<p\s+class="title"[^>]*>\s*<a[^>]+title=["\']([^"\']+)["\']',
        block, re.IGNORECASE,
    )
    if not title_m:
        # fallback: هر a با title طولانی
        title_m = re.search(
            r'<a[^>]+title=["\']([^"\']{10,})["\']',
            block, re.IGNORECASE,
        )
    if title_m:
        title = _clean_html(title_m.group(1))

    if not title:
        return None

    # ─── URL ────────────────────────────────────────
    url = ""
    url_m = re.search(
        r'href=["\'](/video\.[^"\']+)["\']',
        block, re.IGNORECASE,
    )
    if url_m:
        url = f"{_BASE_URL}{url_m.group(1)}"
    else:
        url = f"{_BASE_URL}/video.{eid}/"

    # ─── Thumbnail ──────────────────────────────────
    thumbnail = ""
    thumb_m = re.search(
        r'data-src=["\']([^"\']+)["\']',
        block, re.IGNORECASE,
    )
    if thumb_m:
        thumbnail = thumb_m.group(1)

    # ─── Preview video ──────────────────────────────
    preview = ""
    pvv_m = re.search(
        r'data-pvv=["\']([^"\']+)["\']',
        block, re.IGNORECASE,
    )
    if pvv_m:
        preview = pvv_m.group(1)

    # ─── Duration ───────────────────────────────────
    duration = ""
    dur_m = re.search(
        r'<span\s+class="duration"[^>]*>([^<]+)</span>',
        block, re.IGNORECASE,
    )
    if dur_m:
        duration = dur_m.group(1).strip()

    # ─── Quality (HD mark) ──────────────────────────
    quality = ""
    hd_m = re.search(
        r'<span\s+class="video-hd-mark"[^>]*>([^<]+)</span>',
        block, re.IGNORECASE,
    )
    if hd_m:
        quality = hd_m.group(1).strip()

    # ─── Views ──────────────────────────────────────
    views = ""
    # metadata section: "14.8M Views"
    views_m = re.search(
        r'([\d,.]+\s*[KkMm]?)\s*<span[^>]*>Views</span>',
        block, re.IGNORECASE,
    )
    if not views_m:
        # simpler: number followed by Views
        views_m = re.search(
            r'([\d,.]+\s*[KkMm]?)\s*(?:<[^>]*>)*\s*Views',
            block, re.IGNORECASE,
        )
    if not views_m:
        # just a big number in metadata
        meta_m = re.search(
            r'class="metadata"[^>]*>(.*?)</p>',
            block, re.DOTALL | re.IGNORECASE,
        )
        if meta_m:
            meta_text = re.sub(r'<[^>]+>', ' ', meta_m.group(1))
            num_m = re.search(r'([\d,.]+\s*[KkMm])', meta_text)
            if num_m:
                views = num_m.group(1).strip()
    if views_m:
        views = views_m.group(1).strip()

    # ─── Channel/Profile ────────────────────────────
    channel = ""
    chan_m = re.search(
        r'<span\s+class="name"[^>]*>([^<]+)</span>',
        block, re.IGNORECASE,
    )
    if chan_m:
        channel = chan_m.group(1).strip()

    return XvideosVideo(
        title=title,
        url=url,
        thumbnail=thumbnail,
        preview_video=preview,
        duration=duration,
        views=views,
        quality=quality,
        video_id=vid_id,
        eid=eid,
        channel=channel,
    )


def _clean_html(text: str) -> str:
    """تمیز کردن HTML entities."""
    text = text.replace("&#039;", "'")
    text = text.replace("&amp;", "&")
    text = text.replace("&quot;", '"')
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&#124;", "|")
    text = text.replace("&nbsp;", " ")
    return text.strip()


# ─── Quick test ─────────────────────────────────────────────


async def _test():
    import json as _json

    print("Testing XVideos search...\n")

    results = await search_xvideos("stepsis", page=1, limit=10)
    print(f"Found {len(results)} results:\n")
    for i, v in enumerate(results):
        print(f"  [{i+1}] {v['title'][:70]}")
        print(f"      URL: {v['url']}")
        print(f"      Thumb: {v['thumbnail'][:80]}")
        print(f"      Duration: {v['duration']} | Views: {v['views']} | Quality: {v['quality']}")
        print(f"      Channel: {v['channel']} | ID: {v['video_id']}")
        print()

    # تست sort
    print("\n--- Most Recent ---")
    recent = await search_xvideos("stepsis", sort="mr", limit=3)
    for v in recent:
        print(f"  {v['title'][:60]} [{v['duration']}]")

    print("\n--- Most Viewed ---")
    viewed = await search_xvideos("stepsis", sort="mv", limit=3)
    for v in viewed:
        print(f"  {v['title'][:60]} [{v['views']}]")

    print("\n--- HD Only ---")
    hd = await search_xvideos("stepsis", hd=True, limit=3)
    for v in hd:
        print(f"  {v['title'][:60]} [{v['quality']}]")

    print("\n--- Page 2 ---")
    p2 = await search_xvideos("stepsis", page=2, limit=3)
    for v in p2:
        print(f"  {v['title'][:60]}")

    # Save full results
    with open("xvideos_search_results.json", "w", encoding="utf-8") as f:
        _json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nFull results saved to: xvideos_search_results.json")


if __name__ == "__main__":
    asyncio.run(_test())
