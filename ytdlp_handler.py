import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("YtdlpHandler")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ytdlp_sessions: Dict[str, dict] = {}


def is_xhamster_url(url: str) -> bool:
    return "xhamster.com" in url.lower() or "xhamster.desi" in url.lower()


def is_xvideos_url(url: str) -> bool:
    return "xvideos.com" in url.lower()


def is_pornhub_url(url: str) -> bool:
    return "pornhub.com" in url.lower()


def is_inxxx_url(url: str) -> bool:
    return "inxxx.com" in url.lower() or "inxxx.eu" in url.lower()


def is_hentaiheaven_url(url: str) -> bool:
    return "hentaiheaven.com" in url.lower()


def is_tube8_url(url: str) -> bool:
    return "tube8.com" in url.lower()


def is_pornhat_url(url: str) -> bool:
    return "pornhat.com" in url.lower()


def is_ytdlp_site_url(url: str) -> bool:
    return any(
        [
            is_xhamster_url(url),
            is_xvideos_url(url),
            is_pornhub_url(url),
            is_inxxx_url(url),
            is_hentaiheaven_url(url),
            is_tube8_url(url),
            is_pornhat_url(url),
        ]
    )


def get_site_name(url: str) -> str:
    if is_xhamster_url(url):
        return "xhamster"
    if is_xvideos_url(url):
        return "xvideos"
    if is_pornhub_url(url):
        return "pornhub"
    if is_inxxx_url(url):
        return "inxxx"
    if is_hentaiheaven_url(url):
        return "hentaiheaven"
    if is_tube8_url(url):
        return "tube8"
    if is_pornhat_url(url):
        return "pornhat"
    return "video"


async def extract_qualities_ytdlp(url: str) -> Tuple[List[dict], str]:
    """
    Extract available qualities using yt-dlp --dump-json.
    Returns:
        (qualities, title)
        qualities: list of dict with keys: label, format_id, method, ext, height
        title: video title
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--no-warnings",
            "--dump-json",
            "--skip-download",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            stderr_text = stderr.decode(errors="replace")[:300]
            return [], f"yt-dlp error: {stderr_text}"

        try:
            data = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError:
            return [], "Failed to parse yt-dlp output"
    except FileNotFoundError:
        return [], "yt-dlp not found on server"
    except Exception as e:
        return [], str(e)[:120]

    title = data.get("title", "") or ""
    if title:
        title = re.sub(
            r"\s*[-|]\s*(xhamster|xvideos|pornhub|inxxx|hentaiheaven|tube8|pornhat)\.?(com|eu)?\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()

    qualities = []
    seen_formats = set()

    formats = data.get("formats", [])
    if not formats:
        return [], "No formats found"

    formats.sort(
        key=lambda f: (f.get("height", 0) or 0, f.get("tbr", 0) or 0), reverse=True
    )

    for f in formats:
        vcodec = f.get("vcodec", "none")
        if vcodec == "none":
            continue

        format_id = f.get("format_id", "")
        height = f.get("height", 0) or 0
        ext = f.get("ext", "mp4")
        tbr = f.get("tbr", 0) or 0
        filesize = f.get("filesize", 0) or f.get("filesize_approx", 0)
        fps = f.get("fps", 0) or 0

        res_key = height or format_id
        if res_key in seen_formats:
            continue
        seen_formats.add(res_key)

        if height:
            label = f"🎥 {height}p"
        elif tbr:
            label = f"🎥 ~{int(tbr)}kbps"
        else:
            label = f"🎥 {format_id}"

        if fps > 30:
            label += f" {int(fps)}fps"
        if ext and ext not in ("mp4", "webm"):
            label += f" ({ext})"
        if filesize > 0:
            label += f" [{filesize // 1024 // 1024}MB]"

        qualities.append(
            {
                "label": label,
                "format_id": format_id,
                "method": "ytdlp",
                "ext": ext if ext else "mp4",
                "height": height,
            }
        )

    return qualities, title


async def download_with_ytdlp(
    url: str,
    format_id: str,
    filepath: str,
    progress_cb,
) -> Tuple[bool, str, int]:
    """Download video using yt-dlp with the selected format."""
    await progress_cb("📥 **در حال دانلود با yt-dlp...**")
    try:
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--quiet",
            "--progress",
            "--newline",
            "-f",
            format_id,
            "--add-header",
            f"User-Agent:{USER_AGENT}",
            "-o",
            filepath,
            url,
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
                await progress_cb(f"📥 **Downloading...**\n`{text[:80]}`")

        await process.wait()
        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            return False, stderr[:200], 0

        if not os.path.exists(filepath):
            for ext in [".mp4", ".mkv", ".webm"]:
                alt = (
                    filepath.replace(".mp4", ext)
                    if ".mp4" in filepath
                    else filepath + ext
                )
                if os.path.exists(alt):
                    os.rename(alt, filepath)
                    break

        if not os.path.exists(filepath):
            return False, "Output file not found", 0

        size = os.path.getsize(filepath)
        return True, "", size
    except Exception as e:
        return False, str(e)[:150], 0
