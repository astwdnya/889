"""
pornhub_handler.py
──────────────────
استخراج و دانلود ویدیو از PornHub.com

ویژگی‌ها:
  - استخراج کیفیت‌های مختلف با yt-dlp --dump-json
  - دانلود مستقیم با format_id (بدون merge)
  - پشتیبانی از M3U8 (HLS)
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("PornHubHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

pornhub_sessions: Dict[str, dict] = {}

_PH_REFERER = "https://www.pornhub.com/"


def is_pornhub_url(url: str) -> bool:
    return "pornhub.com" in url.lower()


async def _run_ytdlp_json(url: str) -> Tuple[Optional[dict], Optional[str]]:
    """اجرای yt-dlp --dump-json و برگردوندن خروجی parse شده"""
    try:
        process = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--no-warnings",
            "--no-check-certificate",
            "--dump-json",
            "--no-playlist",
            "--user-agent",
            _USER_AGENT,
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            err = stderr.decode("utf-8", errors="replace")[:200]
            return None, err
        data = json.loads(stdout.decode("utf-8", errors="replace"))
        return data, None
    except Exception as e:
        return None, str(e)[:200]


async def extract_pornhub_qualities(url: str) -> Tuple[List[dict], str]:
    """
    استخراج کیفیت‌های مختلف با yt-dlp --dump-json

    Returns:
        (qualities, title)
        qualities: لیست dict با کلیدهای: label, url, format_id, method, height
        title: عنوان ویدیو
    """
    data, error = await _run_ytdlp_json(url)
    if error or not data:
        return [], error or "Failed to extract data"

    title = data.get("title", "") or data.get("fulltitle", "") or "PornHub Video"
    duration = int(data.get("duration", 0))
    dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""

    qualities = []
    formats = data.get("formats", [])

    if not formats:
        return [], "No formats found"

    seen_heights = set()

    for fmt in formats:
        height = fmt.get("height", 0)
        ext = fmt.get("ext", "mp4")
        protocol = fmt.get("protocol", "")
        format_id = fmt.get("format_id", "")
        vcodec = fmt.get("vcodec", "none")
        filesize = fmt.get("filesize", 0) or fmt.get("filesize_approx", 0)

        if vcodec == "none":
            continue
        if height == 0 and "hls" not in protocol:
            continue
        if height in seen_heights and protocol != "m3u8_native":
            continue

        is_hls = "m3u8" in protocol or "hls" in protocol

        if not is_hls:
            seen_heights.add(height)

        if height:
            label = f"{height}p"
        else:
            label = format_id

        if is_hls:
            label += " [HLS]"
        if filesize:
            size_mb = filesize / (1024 * 1024)
            label += f" ({size_mb:.0f}MB)"

        qualities.append(
            {
                "label": label,
                "url": url,
                "format_id": format_id,
                "method": "ytdlp",
                "height": height,
                "is_hls": is_hls,
                "ext": ext,
            }
        )

    qualities.sort(key=lambda q: (q["is_hls"], -q["height"]))

    if not qualities:
        return [], "No video formats found"

    return qualities, title


async def download_pornhub_video(
    url: str,
    format_id: str,
    output_folder: str,
    progress_callback=None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    دانلود ویدیو از PornHub با format_id مشخص
    """
    session_tag = f"ph_{int(time.time())}"
    output_path = os.path.join(output_folder, f"{session_tag}_%(title)s.%(ext)s")

    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--no-check-certificate",
            "--format",
            format_id,
            "--concurrent-fragments",
            "8",
            "--retries",
            "10",
            "--fragment-retries",
            "10",
            "--add-header",
            f"Referer:{_PH_REFERER}",
            "--add-header",
            f"User-Agent:{_USER_AGENT}",
            "--merge-output-format",
            "mp4",
            "--remux-video",
            "mp4",
            "--output",
            output_path,
            "--no-playlist",
            url,
        ]

        try:
            cmd += ["--impersonate", "chrome"]
        except Exception:
            pass

        logger.info(f"[PORNHUB] Starting download format={format_id}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        last_update = 0.0
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            now = time.time()
            if "[download]" in text and "%" in text and now - last_update >= 2.0:
                last_update = now
                match = re.search(r"(\d+\.?\d*)%", text)
                if match and progress_callback:
                    try:
                        pct = float(match.group(1))
                        await progress_callback(
                            f"📥 Downloading: {pct:.1f}%\n{text[:80]}"
                        )
                    except Exception:
                        pass

        await process.wait()

        if process.returncode != 0:
            rest = await process.stdout.read()
            err = rest.decode(errors="replace")[:200]
            return None, err or "yt-dlp failed"

        downloaded_file = None
        for fname in os.listdir(output_folder):
            if fname.startswith(session_tag):
                fpath = os.path.join(output_folder, fname)
                if os.path.isfile(fpath):
                    downloaded_file = fpath
                    break

        if not downloaded_file:
            for fname in os.listdir(output_folder):
                fpath = os.path.join(output_folder, fname)
                if os.path.isfile(fpath) and fname.endswith((".mp4", ".mkv", ".webm")):
                    if time.time() - os.path.getmtime(fpath) < 120:
                        downloaded_file = fpath
                        break

        if not downloaded_file:
            return None, "Downloaded file not found"

        size = os.path.getsize(downloaded_file)
        if size < 1024:
            return None, f"File too small ({size} bytes)"

        logger.info(f"[PORNHUB] Done: {downloaded_file} ({size} bytes)")
        return downloaded_file, None

    except Exception as e:
        logger.error(f"[PORNHUB] Error: {e}", exc_info=True)
        return None, str(e)[:200]
