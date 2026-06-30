"""
pornhub_handler.py
──────────────────
استخراج و دانلود ویدیو از PornHub.com

ویژگی‌ها:
  - استخراج کیفیت‌های مختلف از صفحه ویدیو
  - دانلود با yt-dlp (چون PornHub رو خوب ساپورت می‌کنه)
  - پشتیبانی از M3U8 و MP4 مستقیم
"""

import asyncio
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("PornHubHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# session های در حال انتظار: key = session_id
pornhub_sessions: Dict[str, dict] = {}


def is_pornhub_url(url: str) -> bool:
    """چک می‌کنه که آیا URL از PornHub هست یا نه"""
    return "pornhub.com" in url.lower()


async def extract_pornhub_qualities(url: str) -> Tuple[List[dict], str]:
    """
    استخراج کیفیت‌های مختلف از صفحه PornHub

    Returns:
        (qualities, title)
        qualities: لیست dict با کلیدهای: label, url, method ('ytdlp')
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

    # عنوان ویدیو از meta tag
    title = ""
    title_m = re.search(
        r'property=["\']og:title["\']\s+content=["\']([^"\']+)',
        html,
        re.IGNORECASE,
    )
    if not title_m:
        title_m = re.search(
            r'content=["\']([^"\']+)["\']\s+property=["\']og:title',
            html,
            re.IGNORECASE,
        )
    if not title_m:
        title_m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)

    if title_m:
        title = title_m.group(1).strip()
        # پاک کردن "- Pornhub.com" از آخر
        title = re.sub(
            r"\s*[-|]\s*Pornhub\.com.*$", "", title, flags=re.IGNORECASE
        ).strip()

    qualities = []

    # PornHub از yt-dlp خوب ساپورت میشه، پس از همون استفاده می‌کنیم
    # کیفیت‌های مختلف رو به صورت گزینه می‌دیم

    # استخراج duration برای نمایش
    duration = ""
    dur_m = re.search(
        r'property=["\']og:duration["\']\s+content=["\']([^"\']+)',
        html,
        re.IGNORECASE,
    )
    if not dur_m:
        dur_m = re.search(
            r'content=["\']([^"\']+)["\']\s+property=["\']og:duration',
            html,
            re.IGNORECASE,
        )
    if dur_m:
        duration = dur_m.group(1).strip()

    # کیفیت‌های استاندارد PornHub
    quality_options = [
        {"label": "🎬 Best Quality (yt-dlp)", "format": "best", "method": "ytdlp"},
        {"label": "📺 1080p (if available)", "format": "1080", "method": "ytdlp"},
        {"label": "📺 720p (if available)", "format": "720", "method": "ytdlp"},
        {"label": "📺 480p", "format": "480", "method": "ytdlp"},
        {"label": "📺 360p", "format": "360", "method": "ytdlp"},
        {"label": "📺 240p (small size)", "format": "240", "method": "ytdlp"},
    ]

    for opt in quality_options:
        qualities.append(
            {
                "label": opt["label"] + (f" [{duration}]" if duration else ""),
                "url": url,  # URL اصلی رو نگه می‌داریم
                "format": opt["format"],
                "method": opt["method"],
            }
        )

    if not qualities:
        return [], "No qualities found"

    return qualities, title


async def download_pornhub_ytdlp(
    url: str,
    quality_format: str,
    output_folder: str,
    progress_callback=None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    دانلود ویدیو از PornHub با yt-dlp

    Args:
        url: لینک ویدیو
        quality_format: کیفیت مورد نظر (best, 1080, 720, 480, 360, 240)
        output_folder: پوشه ذخیره
        progress_callback: تابع callback برای نمایش پیشرفت

    Returns:
        (filepath, error_message)
    """
    try:
        # ساخت format selector بر اساس کیفیت
        if quality_format == "best":
            format_selector = "bestvideo+bestaudio/best"
        else:
            # مثلاً برای 720: "bestvideo[height<=720]+bestaudio/best[height<=720]"
            format_selector = f"bestvideo[height<={quality_format}]+bestaudio/best[height<={quality_format}]"

        # نام فایل خروجی
        output_template = os.path.join(output_folder, "%(title)s_%(height)sp.%(ext)s")

        cmd = [
            "yt-dlp",
            "--no-check-certificate",
            "--no-warnings",
            "--format",
            format_selector,
            "--merge-output-format",
            "mp4",
            "--output",
            output_template,
            "--no-playlist",
            "--user-agent",
            _USER_AGENT,
            url,
        ]

        logger.info(f"[PORNHUB] Running yt-dlp: {' '.join(cmd[:6])}...")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        last_progress = 0.0
        output_lines = []

        while True:
            line = await process.stdout.readline()
            if not line:
                break

            line_str = line.decode("utf-8", errors="replace").strip()
            output_lines.append(line_str)

            # پیدا کردن progress از output yt-dlp
            if "[download]" in line_str and "%" in line_str:
                match = re.search(r"(\d+\.?\d*)%", line_str)
                if match and progress_callback:
                    try:
                        progress = float(match.group(1))
                        now = time.time()
                        if now - last_progress >= 2.0:
                            last_progress = now
                            await progress_callback(
                                f"📥 Downloading: {progress:.1f}%\n{line_str[:80]}"
                            )
                    except Exception:
                        pass

        await process.wait()

        if process.returncode != 0:
            error_output = "\n".join(output_lines[-10:])
            logger.error(f"[PORNHUB] yt-dlp failed: {error_output}")
            return None, f"Download failed: {error_output[:200]}"

        # پیدا کردن فایل دانلود شده
        # yt-dlp معمولاً در آخرین خط می‌گه کجا merge کرده
        downloaded_file = None
        for line in reversed(output_lines):
            if "Merging formats into" in line or "has already been downloaded" in line:
                # استخراج نام فایل
                match = re.search(r'"([^"]+)"', line)
                if match:
                    downloaded_file = match.group(1)
                    break
            elif line.startswith("[download] Destination:"):
                match = re.search(r"\[download\] Destination:\s*(.+)", line)
                if match:
                    downloaded_file = match.group(1).strip()
                    break

        if not downloaded_file or not os.path.exists(downloaded_file):
            # جستجو در پوشه output
            for fname in os.listdir(output_folder):
                fpath = os.path.join(output_folder, fname)
                if os.path.isfile(fpath) and fname.endswith((".mp4", ".mkv", ".webm")):
                    # چک کنیم که فایل تازه ایجاد شده باشه (کمتر از 2 دقیقه)
                    if time.time() - os.path.getmtime(fpath) < 120:
                        downloaded_file = fpath
                        break

        if not downloaded_file or not os.path.exists(downloaded_file):
            return None, "Downloaded file not found"

        file_size = os.path.getsize(downloaded_file)
        if file_size < 1024:
            return None, f"File too small ({file_size} bytes)"

        logger.info(
            f"[PORNHUB] Download complete: {downloaded_file} ({file_size} bytes)"
        )
        return downloaded_file, None

    except Exception as e:
        logger.error(f"[PORNHUB] Download error: {e}", exc_info=True)
        return None, str(e)[:200]


# Aliases برای سازگاری با ساختار bot
download_pornhub_direct = download_pornhub_ytdlp
download_pornhub_m3u8 = download_pornhub_ytdlp
