"""
xnxx_handler.py
---------------
استخراج لینک‌های دانلود از xnxx.com و ارسال ویدیو به کاربر.

روش کار:
  - لینک‌های مستقیم MP4 (360p, 240p) از HTML صفحه استخراج میشن
  - M3U8 stream ها (1080p, 720p, 480p) با yt-dlp دانلود میشن
  - کاربر با دکمه کیفیت انتخاب میکنه
"""

import asyncio
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("XNXXHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# session های در حال انتظار: key = session_id
xnxx_sessions: Dict[str, dict] = {}


def is_xnxx_url(url: str) -> bool:
    return "xnxx.com" in url.lower()


async def extract_xnxx_qualities(url: str) -> Tuple[List[dict], str]:
    """
    لینک‌های کیفیت مختلف رو از صفحه xnxx استخراج میکنه.

    Returns:
        (qualities, title)
        qualities: لیست dict با کلیدهای: label, url, method ('direct' یا 'm3u8')
        title: عنوان ویدیو
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        timeout = ClientTimeout(total=30, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return [], f"HTTP {resp.status}"
                html = await resp.text(errors="replace")
    except Exception as e:
        return [], str(e)[:120]

    # عنوان ویدیو
    title = ""
    title_m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if title_m:
        title = title_m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*XNXX\.COM.*$", "", title, flags=re.IGNORECASE).strip()

    qualities = []

    # ── لینک‌های مستقیم MP4 ──────────────────────────────────
    # setVideoUrl(360, '...') / html5video.setVideoUrlLow(...)
    for pattern in [
        r"setVideoUrl\s*\(\s*(\d+)\s*,\s*'([^']+)'",
        r"setVideoUrlHigh\s*\(\s*'([^']+)'",
        r"setVideoUrlLow\s*\(\s*'([^']+)'",
        r'html5video\.(?:setVideoUrl|mp4)\s*[=(]\s*["\']([^"\']+\.mp4[^"\']*)["\']',
    ]:
        for m in re.finditer(pattern, html):
            if m.lastindex == 2:
                quality_num = m.group(1)
                video_url = m.group(2)
                label = f"🎥 MP4 {quality_num}p"
            else:
                video_url = m.group(1)
                label = "🎥 MP4"
            video_url = video_url.replace("\\/", "/")
            if video_url.startswith("//"):
                video_url = "https:" + video_url
            if not video_url.startswith("http"):
                continue
            # جلوگیری از تکراری
            if any(q["url"] == video_url for q in qualities):
                continue
            qualities.append({
                "label": label,
                "url": video_url,
                "method": "direct",
            })

    # ── M3U8 stream ها ───────────────────────────────────────
    m3u8_patterns = [
        r"setVideoHLS\s*\(\s*'([^']+\.m3u8[^']*)'",
        r'"hls"\s*:\s*"([^"]+\.m3u8[^"]*)"',
        r"'hls'\s*:\s*'([^']+\.m3u8[^']*)'",
        r'url\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
    ]
    for pattern in m3u8_patterns:
        for m in re.finditer(pattern, html):
            m3u8_url = m.group(1).replace("\\/", "/")
            if m3u8_url.startswith("//"):
                m3u8_url = "https:" + m3u8_url
            if not m3u8_url.startswith("http"):
                continue
            if any(q["url"] == m3u8_url for q in qualities):
                continue
            # سعی کنیم کیفیت‌های مختلف M3U8 رو بخونیم
            sub_qualities = await _parse_m3u8_variants(m3u8_url)
            if sub_qualities:
                for sq in sub_qualities:
                    if not any(q["url"] == sq["url"] for q in qualities):
                        qualities.append(sq)
            else:
                qualities.append({
                    "label": "📡 M3U8 Stream",
                    "url": m3u8_url,
                    "method": "m3u8",
                })

    # مرتب‌سازی: کیفیت بالاتر اول
    def _sort_key(q):
        nums = re.findall(r"\d+", q["label"])
        return int(nums[-1]) if nums else 0
    qualities.sort(key=_sort_key, reverse=True)

    return qualities, title


async def _parse_m3u8_variants(master_url: str) -> List[dict]:
    """
    M3U8 master playlist رو پارس میکنه و کیفیت‌های مختلف رو برمیگردونه.
    """
    headers = {"User-Agent": _USER_AGENT}
    try:
        timeout = ClientTimeout(total=15, connect=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(master_url, headers=headers) as resp:
                if resp.status != 200:
                    return []
                content = await resp.text(errors="replace")
    except Exception:
        return []

    # اگه master playlist نیست (stream مستقیم)
    if "#EXT-X-STREAM-INF" not in content:
        return []

    base_url = master_url.rsplit("/", 1)[0] + "/"
    results = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            # استخراج RESOLUTION
            res_m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            bw_m = re.search(r"BANDWIDTH=(\d+)", line)
            if i + 1 < len(lines):
                stream_uri = lines[i + 1].strip()
                if stream_uri and not stream_uri.startswith("#"):
                    if not stream_uri.startswith("http"):
                        stream_uri = base_url + stream_uri
                    if res_m:
                        height = int(res_m.group(2))
                        label = f"📡 M3U8 {height}p"
                    elif bw_m:
                        bw_kb = int(bw_m.group(1)) // 1000
                        label = f"📡 M3U8 ~{bw_kb}kbps"
                    else:
                        label = "📡 M3U8 Stream"
                    results.append({
                        "label": label,
                        "url": stream_uri,
                        "method": "m3u8",
                    })
        i += 1

    return results


async def download_xnxx_direct(
    url: str,
    filepath: str,
    progress_cb,
) -> Tuple[bool, str, int]:
    """
    دانلود لینک مستقیم MP4.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.xnxx.com/",
    }
    try:
        timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}", 0
                content_length = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                start_time = time.time()
                last_update = 0.0
                async with aiofiles.open(filepath, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if now - last_update >= 2.0:
                            last_update = now
                            elapsed = now - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0
                            if content_length > 0:
                                pct = downloaded / content_length * 100
                                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                                text = (
                                    f"📥 **Downloading...**\n`[{bar}]`\n"
                                    f"💾 {downloaded/1024/1024:.1f}/{content_length/1024/1024:.1f} MB"
                                    f"  •  ⚡ {speed/1024/1024:.1f} MB/s\n📊 {pct:.1f}%"
                                )
                            else:
                                text = (
                                    f"📥 **Downloading...**\n"
                                    f"💾 {downloaded/1024/1024:.1f} MB  •  ⚡ {speed/1024/1024:.1f} MB/s"
                                )
                            await progress_cb(text)
        size = os.path.getsize(filepath)
        return True, "", size
    except Exception as e:
        return False, str(e)[:150], 0


async def download_xnxx_m3u8(
    m3u8_url: str,
    filepath: str,
    progress_cb,
) -> Tuple[bool, str, int]:
    """
    دانلود M3U8 stream با yt-dlp.
    """
    await progress_cb("📡 **دانلود M3U8 stream...**")
    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--quiet",
            "--progress",
            "--newline",
            "-f", "best",
            "--hls-prefer-native",
            "--add-header", f"Referer:https://www.xnxx.com/",
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "-o", filepath,
            m3u8_url,
        ]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        last_update = 0.0
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            now = time.time()
            if now - last_update >= 2.0 and text:
                last_update = now
                await progress_cb(f"📡 **Downloading M3U8...**\n`{text[:80]}`")

        await process.wait()
        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            return False, stderr[:200], 0

        if not os.path.exists(filepath):
            # yt-dlp ممکنه پسوند اضافه کنه
            for ext in [".mp4", ".mkv", ".webm"]:
                alt = filepath.replace(".mp4", ext) if ".mp4" in filepath else filepath + ext
                if os.path.exists(alt):
                    os.rename(alt, filepath)
                    break

        if not os.path.exists(filepath):
            return False, "Output file not found", 0

        size = os.path.getsize(filepath)
        return True, "", size
    except Exception as e:
        return False, str(e)[:150], 0
