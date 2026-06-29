"""
playvids_handler.py
-------------------
استخراج و دانلود از playvids.com.

روش کار:
  1. صفحه‌ی embed رو می‌گیریم (تمیزتر از صفحه اصلیه)
  2. لینک userscontent با فرمت ".urlset/master.m3u8" رو پیدا می‌کنیم
  3. توکن seclink/sectime زمان‌داره → &amp; رو تمیز می‌کنیم
  4. دانلود با yt-dlp (HLS multi-bitrate؛ yt-dlp فرمت urlset رو می‌فهمه)

نکته: توکن زمان‌داره → extract باید بلافاصله قبل از دانلود صدا زده بشه.
"""

import asyncio
import html as html_lib
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("PlayvidsHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024

_SITE_DOMAIN = "playvids.com"
_SITE_URL = "https://www.playvids.com"
_SITE_REFERER = f"{_SITE_URL}/"

_ALLOWED_HOSTS = frozenset({"playvids.com", "www.playvids.com"})

# دامنه‌های CDN رسانه
_ALLOWED_HOST_SUFFIXES = (
    ".playvids.com",
    ".userscontent.net",
)

ProgressCallback = Callable[[str], Awaitable[None]]

# الگوی شناسه ویدیو در URL
_ID_RE = re.compile(r"playvids\.com/(?:[a-z]{2}/)?(?:embed/)?([A-Za-z0-9]+)")


# ─── Utility ────────────────────────────────────────────────


def is_playvids_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(f".{_SITE_DOMAIN}")
    except Exception:
        return False


def _is_allowed_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or any(
            host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES
        )
    except Exception:
        return False


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _check_impersonation_support() -> bool:
    try:
        import curl_cffi  # noqa: F401

        return True
    except ImportError:
        return False


def _find_output_file(filepath: str) -> Optional[str]:
    if os.path.exists(filepath):
        return filepath
    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = base + ext
        if os.path.exists(candidate):
            try:
                os.rename(candidate, filepath)
                return filepath
            except OSError:
                return candidate
    return None


def _extract_video_id(url: str) -> Optional[str]:
    m = _ID_RE.search(url)
    return m.group(1) if m else None


def _clean_url(raw: str) -> str:
    """تمیز کردن لینک: &amp; → & و unescape کامل HTML."""
    return html_lib.unescape(raw.strip())


def _extract_title(html: str) -> str:
    m = re.search(
        r'<meta[^>]+og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
    )
    if m:
        return html_lib.unescape(m.group(1).strip())
    m = re.search(r"<title>([^<]+)</title>", html, re.I)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*,?\s*uploaded by.*$", "", title, flags=re.I).strip()
        title = re.sub(r"\s*[-|]\s*PlayVids.*$", "", title, flags=re.I).strip()
        return html_lib.unescape(title) or "Untitled"
    return "Untitled"


def _parse_master_variants(master_body: str, master_url: str) -> List[dict]:
    variants: List[dict] = []
    lines = master_body.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        uri = ""
        for nxt in lines[i + 1 :]:
            nxt = nxt.strip()
            if nxt and not nxt.startswith("#"):
                uri = nxt
                break
        if not uri:
            continue
        res = re.search(r"RESOLUTION=\d+x(\d+)", line)
        bw = re.search(r"BANDWIDTH=(\d+)", line)
        height = int(res.group(1)) if res else 0
        bandwidth = int(bw.group(1)) if bw else 0
        variant_url = _clean_url(urljoin(master_url, uri))
        variants.append({"height": height, "bandwidth": bandwidth, "url": variant_url})
    return variants


# ─── HTTP (curl_cffi) ───────────────────────────────────────


async def _fetch(url: str, referer: str) -> Tuple[Optional[str], int]:
    if not _check_impersonation_support():
        return None, 0
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession() as session:
            try:
                await session.get(_SITE_REFERER, impersonate="chrome", timeout=15)
            except Exception:
                pass
            resp = await session.get(
                url,
                impersonate="chrome",
                headers={"Referer": referer},
                timeout=25,
            )
            return resp.text, resp.status_code
    except Exception as e:
        logger.warning("fetch failed: %s", e)
        return None, 0


# ─── Main extraction ───────────────────────────────────────


def _find_media_url(html: str) -> Optional[str]:
    """لینک userscontent با فرمت urlset/m3u8 یا mp4 رو پیدا می‌کنه."""
    # اولویت با master.m3u8 (multi-bitrate)
    m = re.search(
        r'https?://[^"\'\\\s]*userscontent\.net[^"\'\\\s]*?\.urlset/master\.m3u8[^"\'\\\s]*',
        html,
    )
    if m:
        return _clean_url(m.group(0))
    # fallback: هر m3u8 روی userscontent
    m = re.search(
        r'https?://[^"\'\\\s]*userscontent\.net[^"\'\\\s]*?\.m3u8[^"\'\\\s]*', html
    )
    if m:
        return _clean_url(m.group(0))
    # fallback نهایی: mp4 مستقیم
    m = re.search(
        r'https?://[^"\'\\\s]*userscontent\.net[^"\'\\\s]*?\.mp4[^"\'\\\s]*', html
    )
    if m:
        return _clean_url(m.group(0))
    return None


async def extract_playvids_qualities(url: str) -> Tuple[List[dict], str]:
    """کیفیت‌های مختلف رو از playvids استخراج میکنه (از طریق صفحه embed)."""
    if not is_playvids_url(url):
        return [], "Invalid URL"

    if not _check_impersonation_support():
        return [], "curl_cffi لازمه: pip install curl_cffi"

    vid = _extract_video_id(url)
    if not vid:
        return [], "Video ID پیدا نشد در URL"

    embed_url = f"{_SITE_URL}/embed/{vid}"
    logger.info("Fetching embed: %s", embed_url)
    html, status = await _fetch(embed_url, url)

    media_url = None
    title = "Untitled"

    if html and status == 200:
        media_url = _find_media_url(html)
        title = _extract_title(html)

    if not media_url:
        logger.info("Embed failed, trying main page")
        page_html, pstatus = await _fetch(url, _SITE_REFERER)
        if page_html and pstatus == 200:
            media_url = _find_media_url(page_html)
            if title == "Untitled":
                title = _extract_title(page_html)

    if not media_url:
        return [], "لینک ویدیو پیدا نشد (ساختار سایت تغییر کرده؟)"

    if not _is_allowed_host(media_url):
        logger.warning("Media host not allowed: %s", media_url[:60])
        return [], "میزبان لینک مجاز نیست"

    if ".m3u8" not in media_url:
        return [
            {
                "label": "🎬 دانلود (MP4)",
                "url": media_url,
                "method": "direct",
                "page_url": url,
            }
        ], title

    logger.info("Fetching master playlist: %s", media_url[:80])
    master_body, mstatus = await _fetch(media_url, embed_url)

    qualities: List[dict] = []

    if master_body and "#EXT-X-STREAM-INF" in master_body:
        variants = _parse_master_variants(master_body, media_url)
        variants.sort(key=lambda v: (v["height"], v["bandwidth"]), reverse=True)

        seen = set()
        for v in variants:
            if not _is_allowed_host(v["url"]):
                continue
            if v["url"] in seen:
                continue
            seen.add(v["url"])

            if v["height"]:
                label = f"📡 {v['height']}p"
            elif v["bandwidth"]:
                label = f"📡 {round(v['bandwidth'] / 1000)} kbps"
            else:
                label = "📡 Auto"

            qualities.append(
                {
                    "label": label,
                    "url": v["url"],
                    "method": "m3u8",
                    "page_url": url,
                    "height": v["height"],
                }
            )

    if not qualities:
        logger.info("No variants parsed, using master directly")
        qualities = [
            {
                "label": "📡 دانلود (HLS - بهترین کیفیت)",
                "url": media_url,
                "method": "m3u8",
                "page_url": url,
                "height": 0,
            }
        ]

    logger.info("Extracted %d qualities for: %s", len(qualities), title[:60])
    return qualities, title


# ─── Download (yt-dlp) ──────────────────────────────────────


async def _download_with_ytdlp(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    has_aria2c = shutil.which("aria2c") is not None
    mode = "aria2c" if has_aria2c else "concurrent x16"
    await progress_cb(f"📥 **شروع دانلود (yt-dlp · {mode})...**")

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--progress",
            "--newline",
            "--no-check-certificates",
            "--concurrent-fragments",
            "16",
            "--retries",
            "5",
            "--fragment-retries",
            "5",
            "--retry-sleep",
            "fragment:linear=1:5:2",
            "--buffer-size",
            "16K",
            "--max-filesize",
            str(MAX_DOWNLOAD_SIZE),
            "--add-header",
            f"Referer:{_SITE_REFERER}",
            "--add-header",
            f"Origin:{_SITE_URL}",
            "--add-header",
            f"User-Agent:{_USER_AGENT}",
            "--merge-output-format",
            "mp4",
            "-o",
            filepath,
        ]

        if has_aria2c:
            cmd.extend(
                [
                    "--downloader",
                    "aria2c",
                    "--downloader-args",
                    "aria2c:-x16 -s16 -k1M --max-connection-per-server=16 "
                    "--min-split-size=1M --console-log-level=warn",
                ]
            )

        if _check_impersonation_support():
            cmd.extend(["--impersonate", "chrome"])
        cmd.append(url)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        last_update = 0.0
        tail: List[str] = []
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=180)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                _cleanup_file(filepath)
                return False, "Download timed out", 0
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                tail.append(text)
                if len(tail) > 15:
                    tail.pop(0)
            now = time.time()
            if now - last_update >= 2.0 and text:
                last_update = now
                await progress_cb(f"📥 **Downloading...**\n`{text[:80]}`")

        await process.wait()
        if process.returncode != 0:
            full_err = "\n".join(tail).lower()
            if any(
                p in full_err for p in ("404", "403", "forbidden", "fragment not found")
            ):
                return False, "__EXPIRED__", 0
            err = "\n".join(tail[-5:]) or "yt-dlp failed"
            return False, err[:200], 0

        actual_path = _find_output_file(filepath)
        if not actual_path:
            return False, "Output file not found", 0

        size = os.path.getsize(actual_path)
        if size == 0:
            _cleanup_file(actual_path)
            return False, "Downloaded file is empty", 0
        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(actual_path)
            return False, "File exceeds size limit", 0
        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        return False, str(e)[:150], 0


# ─── Download: Public API ──────────────────────────────────


async def download_playvids_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    page_url: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """
    دانلود از playvids با yt-dlp.

    اگه توکن منقضی شده باشه و page_url داده شده باشه، یک بار خودکار
    دوباره extract می‌کنه و با لینک تازه تلاش می‌کنه.
    """
    if not _is_allowed_host(m3u8_url):
        return False, "URL host not allowed", 0

    success, error, size = await _download_with_ytdlp(m3u8_url, filepath, progress_cb)
    if success:
        return True, "", size

    _cleanup_file(filepath)

    # ── توکن منقضی: یک بار رفرش خودکار ──
    if error == "__EXPIRED__" and page_url and is_playvids_url(page_url):
        await progress_cb("♻️ **توکن منقضی شد، در حال گرفتن لینک تازه...**")
        qualities, _title = await extract_playvids_qualities(page_url)
        if qualities:
            fresh_url = qualities[0]["url"]
            success, error, size = await _download_with_ytdlp(
                fresh_url, filepath, progress_cb
            )
            if success:
                return True, "", size
            _cleanup_file(filepath)

    if error == "__EXPIRED__":
        error = "لینک منقضی شد. لطفاً دوباره لینک ویدیو رو بفرست."

    return False, error, 0


async def download_playvids_direct(
    url: str,
    filepath: str,
    progress_cb: ProgressCallback,
    page_url: Optional[str] = None,
) -> Tuple[bool, str, int]:
    """دانلود مستقیم (برای سازگاری با API دیگر handlerها)."""
    return await download_playvids_m3u8(url, filepath, progress_cb, page_url)
