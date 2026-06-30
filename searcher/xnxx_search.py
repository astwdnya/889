"""
xnxx_search.py
──────────────
سرچ ویدیو از XNXX با scraping.

استفاده:
  from xnxx_search import search_xnxx
  results = await search_xnxx("query", page=0, limit=20)
"""

import asyncio
import logging
import re
from dataclasses import dataclass, asdict
from typing import List, Optional
from urllib.parse import quote_plus

import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("XnxxSearch")

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

_BASE_URL = "https://www.xnxx.com"
_RESULTS_PER_PAGE = 36


# ─── Data model ─────────────────────────────────────────────


@dataclass
class XnxxVideo:
    """یک نتیجه سرچ XNXX."""

    title: str
    url: str
    thumbnail: str
    duration: str
    views: str
    quality: str
    video_id: str
    source: str = "xnxx"

    def to_dict(self) -> dict:
        return asdict(self)


# ─── Search ─────────────────────────────────────────────────


async def search_xnxx(
    query: str,
    page: int = 0,
    limit: int = 20,
    sort: str = "relevance",
) -> List[dict]:
    """
    سرچ ویدیو از XNXX.

    sort options:
      - relevance (default)
      - uploaddate (جدیدترین)
      - rating (بهترین امتیاز)
      - length (طولانی‌ترین)
      - views (بیشترین بازدید)
      - hits (پربازدید - path based)
      - month (ماهانه - path based)

    Returns:
        لیست dict با کلیدهای: title, url, thumbnail, duration, views, quality, video_id, source
    """
    if not query or len(query.strip()) < 2:
        return []

    query = query.strip()
    encoded = quote_plus(query)

    qs_sorts = {"uploaddate", "rating", "length", "views", "relevance"}
    path_sorts = {"hits", "month"}

    if sort in path_sorts:
        if page == 0:
            search_url = f"{_BASE_URL}/search/{sort}/{encoded}"
        else:
            search_url = f"{_BASE_URL}/search/{sort}/{encoded}/{page}"
    elif sort in qs_sorts and sort != "relevance":
        if page == 0:
            search_url = f"{_BASE_URL}/search/{encoded}?sort={sort}"
        else:
            search_url = f"{_BASE_URL}/search/{encoded}/{page}?sort={sort}"
    else:
        if page == 0:
            search_url = f"{_BASE_URL}/search/{encoded}"
        else:
            search_url = f"{_BASE_URL}/search/{encoded}/{page}"

    logger.info(
        "XNXX search: q='%s' page=%d sort=%s url=%s", query, page, sort, search_url
    )

    html = await _fetch_page(search_url)
    if not html:
        return []

    results = _parse_search_results(html)

    if limit and len(results) > limit:
        results = results[:limit]

    logger.info(
        "Found %d results for '%s' (page %d, sort %s)", len(results), query, page, sort
    )
    return [r.to_dict() for r in results]


def parse_inline_query(raw_query: str) -> dict:
    """
    پارس query اینلاین XNXX.

    فرمت‌ها:
      step sis          → سرچ عادی، صفحه 0
      step sis=2        → صفحه 2
      step sis=new      → جدیدترین‌ها (?sort=uploaddate)
      step sis=top      → بهترین امتیاز (?sort=rating)
      step sis=long     → طولانی‌ترین‌ها (?sort=length)
      step sis=views    → بیشترین بازدید (?sort=views)
      step sis=month    → ماهانه (path based)
      step sis=hits     → پربازدید (path based)
    """
    raw_query = raw_query.strip()

    result = {"query": raw_query, "page": 0, "sort": "relevance"}

    m = re.match(r"^(.+?)=(\S+)$", raw_query)
    if not m:
        return result

    query_part = m.group(1).strip()
    param = m.group(2).strip().lower()

    result["query"] = query_part

    if param.isdigit():
        result["page"] = int(param)
    elif param == "new":
        result["sort"] = "uploaddate"
    elif param == "top":
        result["sort"] = "rating"
    elif param == "long":
        result["sort"] = "length"
    elif param in ("views", "hits"):
        result["sort"] = param
    elif param == "month":
        result["sort"] = "month"
    elif param == "best":
        result["sort"] = "rating"

    return result


async def search_xnxx_multi_page(
    query: str,
    pages: int = 3,
    limit: int = 50,
) -> List[dict]:
    """
    سرچ چند صفحه‌ای از XNXX.

    Args:
        query: عبارت جستجو
        pages: تعداد صفحات
        limit: حداکثر کل نتایج
    """
    if not query or len(query.strip()) < 2:
        return []

    tasks = [search_xnxx(query, page=p, limit=_RESULTS_PER_PAGE) for p in range(pages)]
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


# ─── HTTP ───────────────────────────────────────────────────


async def _fetch_page(url: str) -> Optional[str]:
    """دریافت صفحه HTML."""
    timeout = ClientTimeout(total=15, connect=10)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=_HEADERS, allow_redirects=True) as resp:
                if resp.status == 200:
                    return await resp.text(errors="replace")
                logger.warning("HTTP %d for %s", resp.status, url)
                return None
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Fetch failed for %s: %s", url, e)
        return None


# ─── HTML Parsing ───────────────────────────────────────────


def _parse_search_results(html: str) -> List[XnxxVideo]:
    """
    پارس نتایج سرچ از HTML.

    ساختار XNXX:
      <div class="mozaique">
        <div class="thumb-block">
          <div class="thumb-inside">
            <div class="thumb">
              <a href="/video-XXX/...">
                <img data-src="https://thumb-cdn77.xnxx-cdn.com/..." />
              </a>
            </div>
          </div>
          <div class="thumb-under">
            <p class="title">
              <a href="/video-XXX/..." title="Video Title">...</a>
            </p>
            <p class="metadata">
              <span class="right"> 2.6M <span class="icon-f icf-eye">
              </span>100%</span>
              16min - 1080p
            </p>
          </div>
        </div>
      </div>
    """
    results = []

    # div.thumb-inside رو به عنوان مرز بلاک استفاده میکنیم
    # هر ویدیو = یه thumb-inside + thumb-under بعدش
    # بهترین روش: بین thumb-block ها split کنیم
    # ولی چون thumb-block توی لاگ 0 بود، از mozaique content استفاده میکنیم

    # کل محتوای mozaique رو بگیر
    moz_match = re.search(
        r'<div\s+class="mozaique[^"]*"[^>]*>(.*)',
        html,
        re.DOTALL,
    )
    if not moz_match:
        logger.warning("No mozaique container found")
        return results

    moz_html = moz_match.group(1)

    # هر ویدیو دو تا لینک داره: یکی روی تامبنیل، یکی روی عنوان
    # بهترین روش: پیدا کردن همه thumb-inside بلاک‌ها
    # و بعد thumb-under بعدشون

    # استراتژی: پیدا کردن همه video URL های یونیک
    # و برای هر کدوم اطلاعات رو جمع کن
    video_entries = _extract_video_entries(moz_html)

    for entry in video_entries:
        video = _build_video_from_entry(entry)
        if video:
            results.append(video)

    return results


def _extract_video_entries(html: str) -> List[dict]:
    """
    استخراج اطلاعات خام هر ویدیو.
    هر ویدیو شامل: url, title, thumbnail, metadata
    """
    entries = []
    seen_ids = set()

    # پیدا کردن همه لینک‌های ویدیو با title attribute
    # این لینک‌ها توی بخش thumb-under هستن و عنوان کامل دارن
    title_links = re.finditer(
        r'<a\s+href="(/video-([a-z0-9]+)/[^"]*)"'
        r'\s+title="([^"]+)"',
        html,
        re.IGNORECASE,
    )

    for m in title_links:
        video_path = m.group(1)
        video_id = m.group(2)
        title = m.group(3)

        if video_id in seen_ids:
            continue
        seen_ids.add(video_id)

        entry = {
            "video_id": video_id,
            "path": video_path,
            "title": title,
            "thumbnail": "",
            "duration": "",
            "views": "",
            "quality": "",
        }

        # تامبنیل: پیدا کردن img با data-src نزدیک به همین video_id
        thumb_pattern = (
            rf'href="/video-{re.escape(video_id)}/[^"]*"[^>]*>'
            r'\s*<img[^>]+data-src="([^"]+)"'
        )
        thumb_m = re.search(thumb_pattern, html, re.DOTALL)
        if thumb_m:
            entry["thumbnail"] = thumb_m.group(1)
        else:
            # fallback: هر data-src نزدیک video_id
            idx = html.find(f"/video-{video_id}/")
            if idx >= 0:
                # 500 کاراکتر قبلش رو بگرد
                search_area = html[max(0, idx - 500) : idx + 500]
                ds = re.search(r'data-src="([^"]+xnxx-cdn\.com[^"]+)"', search_area)
                if ds:
                    entry["thumbnail"] = ds.group(1)

        # metadata: views, duration, quality
        # بعد از title link، metadata میاد
        title_pos = html.find(f'title="{title}"')
        if title_pos >= 0:
            meta_area = html[title_pos : title_pos + 500]
            meta_m = re.search(
                r'<p\s+class="metadata">(.*?)</p>',
                meta_area,
                re.DOTALL,
            )
            if meta_m:
                meta_html = meta_m.group(1)
                _parse_metadata(meta_html, entry)

        entries.append(entry)

    return entries


def _parse_metadata(meta_html: str, entry: dict) -> None:
    """
    پارس metadata از HTML.
    فرمت: <span class="right"> 2.6M <span class="icon-f icf-eye"></span>100%</span> 16min - 1080p
    """
    # حذف HTML tags
    text = re.sub(r"<[^>]+>", " ", meta_html)
    text = re.sub(r"\s+", " ", text).strip()

    # Views: عدد + M/k قبل از آیکون چشم
    views_m = re.search(r"([\d.]+[MkK]?)\s", text)
    if views_m:
        entry["views"] = views_m.group(1).strip()

    # Duration: Xmin یا XhYmin
    dur_m = re.search(r"(\d+\s*h\s*)?(\d+)\s*min", text, re.IGNORECASE)
    if dur_m:
        hours = dur_m.group(1)
        mins = dur_m.group(2)
        if hours:
            h = re.search(r"\d+", hours).group()
            entry["duration"] = f"{h}h {mins}min"
        else:
            entry["duration"] = f"{mins}min"

    # Quality: 720p, 1080p, 4K, etc.
    qual_m = re.search(r"(\d{3,4}p|[48][kK])", text)
    if qual_m:
        entry["quality"] = qual_m.group(1)


def _build_video_from_entry(entry: dict) -> Optional[XnxxVideo]:
    """ساخت XnxxVideo از entry dict."""
    if not entry.get("path") or not entry.get("title"):
        return None

    url = f"{_BASE_URL}{entry['path']}"

    return XnxxVideo(
        title=_clean_title(entry["title"]),
        url=url,
        thumbnail=entry.get("thumbnail", ""),
        duration=entry.get("duration", ""),
        views=entry.get("views", ""),
        quality=entry.get("quality", ""),
        video_id=entry.get("video_id", ""),
    )


def _clean_title(title: str) -> str:
    """تمیز کردن عنوان."""
    title = title.replace("&#039;", "'")
    title = title.replace("&amp;", "&")
    title = title.replace("&quot;", '"')
    title = title.replace("&lt;", "<")
    title = title.replace("&gt;", ">")
    return title.strip()


# ─── Quick test ─────────────────────────────────────────────


async def _test():
    """تست سریع."""
    print("Testing XNXX search...")
    results = await search_xnxx("japanese step sister", limit=5)
    print(f"\nFound {len(results)} results:\n")
    for i, v in enumerate(results):
        print(f"  [{i + 1}] {v['title'][:70]}")
        print(f"      URL: {v['url'][:80]}")
        print(f"      Thumb: {v['thumbnail'][:80]}")
        print(
            f"      Duration: {v['duration']} | Views: {v['views']} | Quality: {v['quality']}"
        )
        print()


if __name__ == "__main__":
    asyncio.run(_test())
