"""
eporner_search.py
─────────────────
سرچ ویدیو از eporner.com با scraping.

Inline prefix: ep:
فرمت‌ها:
  ep:step sis          → سرچ عادی
  ep:step sis=2        → صفحه 2
  ep:step sis=new      → جدیدترین
  ep:step sis=top=3    → بهترین امتیاز صفحه 3

استفاده:
  from eporner_search import search_eporner
  results = await search_eporner("query", page=1, limit=20)
"""

import asyncio
import html as html_lib
import logging
import re
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import quote_plus

import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("EpornerSearch")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_BASE_URL = "https://www.eporner.com"
_RESULTS_PER_PAGE = 77

_SORT_MAP = {
    "relevance": None,
    "new": "newest",
    "newest": "newest",
    "top": "top-rated",
    "rating": "top-rated",
    "best": "top-rated",
    "long": "longest",
    "longest": "longest",
    "views": "most-viewed",
    "most-viewed": "most-viewed",
}

MAX_RETRIES = 2
RETRY_DELAY = 1.5


# ─── Data model ─────────────────────────────────────────────


@dataclass
class EpornerVideo:
    """یک نتیجه سرچ eporner."""

    title: str
    url: str
    thumbnail: str
    duration: str
    views: str
    rating: str
    quality: str
    video_id: str
    uploader: str = ""
    source: str = "eporner"

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Inline query parser ───────────────────────────────────


def parse_inline_query(raw_query: str) -> dict:
    """
    پارس query اینلاین eporner.

    فرمت‌ها:
      step sis          → سرچ عادی، صفحه 1
      step sis=2        → صفحه 2
      step sis=new      → جدیدترین
      step sis=new=3    → جدیدترین صفحه 3
      step sis=top      → بهترین امتیاز
      step sis=long     → طولانی‌ترین
      step sis=views    → بیشترین بازدید
    """
    raw_query = raw_query.strip()
    result = {"query": raw_query, "page": 1, "sort": "relevance"}

    parts = raw_query.split("=")
    if len(parts) < 2:
        return result

    result["query"] = parts[0].strip()

    sort_val = parts[1].strip().lower()
    page_val = parts[2].strip() if len(parts) >= 3 else ""

    if sort_val.isdigit():
        result["page"] = max(1, int(sort_val))
    elif sort_val in _SORT_MAP:
        result["sort"] = sort_val
        if page_val.isdigit():
            result["page"] = max(1, int(page_val))

    return result


# ─── Search ─────────────────────────────────────────────────


async def search_eporner(
    query: str,
    page: int = 1,
    limit: int = 20,
    sort: str = "relevance",
) -> List[dict]:
    """
    سرچ ویدیو از eporner.

    Args:
        query: عبارت جستجو (حداقل 2 کاراکتر)
        page: شماره صفحه (از 1 شروع میشه)
        limit: حداکثر تعداد نتایج
        sort: نوع مرتب‌سازی (relevance, new, top, long, views)

    Returns:
        لیست dict
    """
    if not query or len(query.strip()) < 2:
        return []

    query = query.strip()
    page = max(1, page)
    limit = max(1, min(limit, _RESULTS_PER_PAGE))

    search_url = _build_search_url(query, page, sort)

    logger.info(
        "Eporner search: q='%s' page=%d sort=%s url=%s",
        query, page, sort, search_url,
    )

    html = await _fetch_page(search_url)
    if not html:
        return []

    results = _parse_search_results(html)

    if len(results) > limit:
        results = results[:limit]

    logger.info(
        "Found %d results for '%s' (page %d, sort %s)",
        len(results), query, page, sort,
    )
    return [r.to_dict() for r in results]


async def search_eporner_multi_page(
    query: str,
    pages: int = 3,
    limit: int = 50,
    sort: str = "relevance",
) -> List[dict]:
    """سرچ چند صفحه‌ای."""
    if not query or len(query.strip()) < 2:
        return []

    pages = max(1, min(pages, 5))

    tasks = [
        search_eporner(query, page=p, limit=_RESULTS_PER_PAGE, sort=sort)
        for p in range(1, pages + 1)
    ]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    combined = []
    seen_ids = set()
    for page_results in all_results:
        if isinstance(page_results, Exception):
            logger.warning("Page search failed: %s", page_results)
            continue
        for video in page_results:
            vid = video.get("video_id", "")
            if vid and vid not in seen_ids:
                seen_ids.add(vid)
                combined.append(video)
            elif not vid:
                combined.append(video)

    return combined[:limit]


# ─── URL builder ────────────────────────────────────────────


def _build_search_url(query: str, page: int, sort: str) -> str:
    """ساخت URL سرچ."""
    encoded = quote_plus(query)

    if page <= 1:
        path = f"{_BASE_URL}/search/{encoded}/"
    else:
        path = f"{_BASE_URL}/search/{encoded}/{page}/"

    order_val = _SORT_MAP.get(sort)
    if order_val:
        path += f"?order={order_val}"

    return path


# ─── HTTP ───────────────────────────────────────────────────


async def _fetch_page(url: str) -> Optional[str]:
    """دریافت صفحه HTML با retry."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=20, connect=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers=_HEADERS, allow_redirects=True
                ) as resp:
                    if resp.status == 200:
                        return await resp.text(errors="replace")
                    if 400 <= resp.status < 500:
                        logger.warning("HTTP %d for %s", resp.status, url)
                        return None
                    logger.warning(
                        "HTTP %d for %s (attempt %d/%d)",
                        resp.status, url, attempt, MAX_RETRIES,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Fetch attempt %d/%d failed: %s", attempt, MAX_RETRIES, e
            )

        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_DELAY * attempt)

    return None


# ─── HTML Parsing ───────────────────────────────────────────


def _parse_search_results(page_html: str) -> List[EpornerVideo]:
    """
    پارس نتایج سرچ از HTML.

    ساختار واقعی eporner:
      <div class="mb" data-id="14550675" data-vp="..." id="vf14550675">
        <div class="mbimg">
          <div class="mbcontent">
            <a href="/video-XXX/slug/">
              <img src="https://static-eu-cdn.eporner.com/thumbs/..." alt="Title" />
            </a>
            <div class="mvhdico" title="Quality"><span>720p</span></div>
          </div>
        </div>
        <div class="mbunder">
          <p class="mbtit">
            <a href="/video-XXX/slug/">Full Title - Pornstar Name</a>
          </p>
          <p class="mbstats">
            <span class="mbtim" title="Duration">39:27</span>
            <span class="mbrate" title="Rating">86%</span>
            <span class="mbvie" title="Views">302,384</span>
            <span class="mb-uploader">
              <a href="/profile/user/" title="Uploader">user</a>
            </span>
          </p>
        </div>
      </div>
    """
    results = []
    seen_ids = set()

    # پیدا کردن همه بلاک‌ها با id=vfXXXXX
    # split بین هر id="vf
    block_starts = list(re.finditer(r'id="vf(\d+)"', page_html))

    for i, match in enumerate(block_starts):
        vid = match.group(1)

        # جلوگیری از تکراری
        if vid in seen_ids:
            continue
        seen_ids.add(vid)

        # محدوده بلاک: از این id تا id بعدی
        start = match.start()
        end = block_starts[i + 1].start() if i + 1 < len(block_starts) else start + 2000
        block_html = page_html[start:end]

        video = _parse_single_block(vid, block_html)
        if video:
            results.append(video)

    return results


def _parse_single_block(video_id: str, block_html: str) -> Optional[EpornerVideo]:
    """پارس یک بلاک ویدیو."""

    # لینک ویدیو
    link_m = re.search(
        r'href="(/video-([a-zA-Z0-9]+)/[^"]*)"', block_html
    )
    if not link_m:
        return None

    video_path = link_m.group(1)
    video_url = f"{_BASE_URL}{video_path}"

    # عنوان: اول از mbtit (عنوان کامل‌تره)، بعد از alt
    title = ""
    mbtit_m = re.search(
        r'class="mbtit">\s*<a[^>]*>([^<]+)</a>', block_html
    )
    if mbtit_m:
        title = html_lib.unescape(mbtit_m.group(1).strip())

    if not title:
        alt_m = re.search(r'alt="([^"]+)"', block_html)
        if alt_m:
            title = html_lib.unescape(alt_m.group(1).strip())

    if not title:
        return None

    # تامبنیل
    thumbnail = ""
    img_m = re.search(
        r'<img[^>]+src="(https://static[^"]+\.jpg)"', block_html
    )
    if img_m:
        thumbnail = img_m.group(1)

    # کیفیت از mvhdico
    quality = ""
    qual_m = re.search(
        r'class="mvhdico"[^>]*>\s*<span>([^<]+)</span>', block_html
    )
    if qual_m:
        quality = qual_m.group(1).strip()

    # Duration از mbtim
    duration = ""
    dur_m = re.search(
        r'class="mbtim"[^>]*>([^<]+)</span>', block_html
    )
    if dur_m:
        duration = dur_m.group(1).strip()

    # Rating از mbrate
    rating = ""
    rate_m = re.search(
        r'class="mbrate"[^>]*>([^<]+)</span>', block_html
    )
    if rate_m:
        rating = rate_m.group(1).strip()

    # Views از mbvie
    views = ""
    views_m = re.search(
        r'class="mbvie"[^>]*>([^<]+)</span>', block_html
    )
    if views_m:
        views = views_m.group(1).strip()

    # Uploader
    uploader = ""
    up_m = re.search(
        r'class="mb-uploader"[^>]*>\s*<a[^>]*>([^<]+)</a>', block_html
    )
    if up_m:
        uploader = up_m.group(1).strip()

    return EpornerVideo(
        title=title,
        url=video_url,
        thumbnail=thumbnail,
        duration=duration,
        views=views,
        rating=rating,
        quality=quality,
        video_id=video_id,
        uploader=uploader,
    )


# ─── Quick test ─────────────────────────────────────────────


async def _test():
    """تست سریع."""
    print("=" * 60)
    print("  EPORNER SEARCH TEST")
    print("=" * 60)

    # تست 1: سرچ عادی
    print("\n--- Test 1: Basic search ---")
    results = await search_eporner("step sister", limit=5)
    print(f"Found {len(results)} results\n")
    for i, v in enumerate(results):
        print(f"  [{i+1}] {v['title'][:70]}")
        print(f"      URL:      {v['url'][:80]}")
        print(f"      Thumb:    {v['thumbnail'][:80]}")
        print(f"      Duration: {v['duration']} | Views: {v['views']} | "
              f"Rating: {v['rating']} | Quality: {v['quality']}")
        print(f"      Uploader: {v['uploader']} | ID: {v['video_id']}")
        print()

    await asyncio.sleep(1)

    # تست 2: صفحه 2
    print("--- Test 2: Page 2 ---")
    results2 = await search_eporner("step sister", page=2, limit=3)
    print(f"Found {len(results2)} results (page 2)\n")
    for v in results2:
        print(f"  - {v['title'][:60]} [{v['duration']}] {v['quality']}")

    await asyncio.sleep(1)

    # تست 3: Sort by newest
    print("\n--- Test 3: Sort by newest ---")
    results3 = await search_eporner("japanese", sort="new", limit=3)
    print(f"Found {len(results3)} results (newest)\n")
    for v in results3:
        print(f"  - {v['title'][:60]} [{v['duration']}] {v['quality']}")

    await asyncio.sleep(1)

    # تست 4: Inline query parser
    print("\n--- Test 4: Inline query parser ---")
    test_queries = [
        "step sis",
        "step sis=2",
        "step sis=new",
        "step sis=top=3",
        "step sis=long",
        "step sis=views",
    ]
    for q in test_queries:
        parsed = parse_inline_query(q)
        print(f"  '{q}' -> query='{parsed['query']}' page={parsed['page']} sort={parsed['sort']}")

    await asyncio.sleep(1)

    # تست 5: Multi-page
    print("\n--- Test 5: Multi-page search ---")
    results5 = await search_eporner_multi_page("teen", pages=2, limit=10)
    print(f"Found {len(results5)} unique results from 2 pages\n")
    for i, v in enumerate(results5):
        print(f"  [{i+1}] {v['title'][:55]} [{v['duration']}] {v['quality']} | {v['views']}")

    print("\n" + "=" * 60)
    print("  ALL TESTS DONE")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(_test())
