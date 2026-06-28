"""
ytdlp_handler.py
---------------
استخراج کیفیت و دانلود ویدیو از سایت‌های مختلف با yt-dlp
"""

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
    return "hentaiheaven.com" in url.lower() or "hentaihaven.xxx" in url.lower()


def is_tube8_url(url: str) -> bool:
    return "tube8.com" in url.lower() or "t8" in url.lower()


def is_pornhat_url(url: str) -> bool:
    return "pornhat.com" in url.lower()


# === New site detectors ===

def is_youporn_url(url: str) -> bool:
    return "youporn.com" in url.lower() or "youporngay.com" in url.lower()


def is_redtube_url(url: str) -> bool:
    return "redtube.com" in url.lower()


def is_eporner_url(url: str) -> bool:
    return "eporner.com" in url.lower()


def is_spankbang_url(url: str) -> bool:
    return "spankbang.com" in url.lower()


def is_beeg_url(url: str) -> bool:
    return "beeg.com" in url.lower()


def is_thisvid_url(url: str) -> bool:
    return "thisvid.com" in url.lower()


def is_brattysis_url(url: str) -> bool:
    return "brattysis.com" in url.lower() or "brattysisters.com" in url.lower()


def is_realitykings_url(url: str) -> bool:
    return "realitykings.com" in url.lower()


def is_xfantazy_url(url: str) -> bool:
    return "xfantazy.com" in url.lower() or "xfantazy.org" in url.lower()


def is_anysex_url(url: str) -> bool:
    return "anysex.com" in url.lower()


def is_tnaflix_url(url: str) -> bool:
    return "tnaflix.com" in url.lower()


def is_momvids_url(url: str) -> bool:
    return "momvids.com" in url.lower()


def is_faapy_url(url: str) -> bool:
    return "faapy.com" in url.lower()


def is_pornid_url(url: str) -> bool:
    return "pornid.xxx" in url.lower() or "pornid.com" in url.lower()


def is_hotmovies_url(url: str) -> bool:
    return "hotmovies.com" in url.lower()


def is_fapcat_url(url: str) -> bool:
    return "fapcat.com" in url.lower()


def is_okxxx_url(url: str) -> bool:
    return "ok.xxx" in url.lower()


def is_pornhex_url(url: str) -> bool:
    return "pornhex.com" in url.lower()


def is_porntrex_url(url: str) -> bool:
    return "porntrex.com" in url.lower()


def is_ytdlp_site_url(url: str) -> bool:
    """Check if URL is supported by ytdlp handler (excluding dedicated handlers)."""
    return any(
        [
            is_inxxx_url(url),
            is_hentaiheaven_url(url),
            is_tube8_url(url),
            is_pornhat_url(url),
            is_youporn_url(url),
            is_redtube_url(url),
            is_eporner_url(url),
            is_spankbang_url(url),
            is_beeg_url(url),
            is_thisvid_url(url),
            is_brattysis_url(url),
            is_realitykings_url(url),
            is_xfantazy_url(url),
            is_anysex_url(url),
            is_tnaflix_url(url),
            is_momvids_url(url),
            is_faapy_url(url),
            is_pornid_url(url),
            is_hotmovies_url(url),
            is_fapcat_url(url),
            is_okxxx_url(url),
            is_pornhex_url(url),
            is_porntrex_url(url),
            # Generic catch for many others
            any(domain in url.lower() for domain in [
                "youx.xxx", "xgroovy", "rat.xxx", "sexvid", "nakedgirls", "hdtube",
                "fapcat", "ok.xxx", "momvids", "xfantazy", "anysex", "tnaflix",
                "porntrex", "pornwhite", "spankbang", "eporner", "brattysis",
                "realitykings", "thisvid", "pornid", "hotmovies", "pornhex",
                "teensexvideos", "44sex", "fetishshrine", "pornozmo", "xxxbp",
                "milfnut", "porntn", "videoxxx", "xfree", "videomonstr", "fapnado",
                "auntymaza", "best18porn", "xlx.xxx", "melons tube", "cartoonporn",
                "porndiff", "uiporn", "perfectgirls", "family therapy"
            ])
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
    if is_youporn_url(url):
        return "youporn"
    if is_redtube_url(url):
        return "redtube"
    if is_eporner_url(url):
        return "eporner"
    if is_spankbang_url(url):
        return "spankbang"
    if is_beeg_url(url):
        return "beeg"
    if is_thisvid_url(url):
        return "thisvid"
    if is_brattysis_url(url):
        return "brattysis"
    if is_realitykings_url(url):
        return "realitykings"
    if is_xfantazy_url(url):
        return "xfantazy"
    if is_anysex_url(url):
        return "anysex"
    if is_tnaflix_url(url):
        return "tnaflix"
    if is_momvids_url(url):
        return "momvids"
    if is_faapy_url(url):
        return "faapy"
    if is_pornid_url(url):
        return "pornid"
    if is_hotmovies_url(url):
        return "hotmovies"
    if is_fapcat_url(url):
        return "fapcat"
    if is_okxxx_url(url):
        return "okxxx"
    if is_pornhex_url(url):
        return "pornhex"
    if is_porntrex_url(url):
        return "porntrex"
    return "video"


async def extract_qualities_ytdlp(url: str) -> Tuple[List[dict], str]:
    """
    Extract available qualities using yt-dlp --dump-json.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "yt-dlp",
            "--no-warnings",
            "--dump-json",
            "--skip-download",
            "--add-header",
            f"User-Agent:{USER_AGENT}",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            return [], "yt-dlp timed out (30s)"
        if process.returncode != 0:
            stderr_text = stderr.decode(errors="replace")[:300]
            return [], f"yt-dlp error: {stderr_text}"

        try:
            data = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError:
            stderr_text = stderr.decode(errors="replace")[:300]
            return [], f"Failed to parse yt-dlp output: {stderr_text}"
    except FileNotFoundError:
        return [], "yt-dlp not found on server"
    except Exception as e:
        return [], str(e)[:120]

    title = data.get("title", "") or ""
    if title:
        title = re.sub(
            r"\s*[-|]\s*(xhamster|xvideos|pornhub|inxxx|hentaihaven|tube8|pornhat|"
            r"youporn|redtube|eporner|spankbang|beeg|thisvid|brattysis|realitykings|"
            r"xfantazy|anysex|tnaflix|momvids|faapy|pornid|hotmovies|fapcat|okxxx|"
            r"pornhex|porntrex)\.?(com|eu|xxx|net|org)?\s*$",
            "",
            title,
            flags=re.IGNORECASE,
        ).strip()

    # ... (بقیه کد extract_qualities_ytdlp بدون تغییر)
    qualities = []
    seen_formats = set()

    formats = data.get("formats", [])
    if not formats:
        direct_url = data.get("url", "")
        if direct_url:
            qualities.append(
                {
                    "label": "🎥 Video (best)",
                    "format_id": "best",
                    "method": "ytdlp",
                    "ext": "mp4",
                    "height": 0,
                }
            )
            return qualities, title if title else "video"
        return [], "No formats found"

    formats.sort(
        key=lambda f: (f.get("height", 0) or 0, f.get("tbr", 0) or 0), reverse=True
    )

    for f in formats:
        vcodec = f.get("vcodec") or ""
        height = f.get("height", 0) or 0

        if not vcodec and height == 0:
            continue

        format_id = f.get("format_id", "")
        ext = f.get("ext", "mp4")
        tbr = f.get("tbr", 0) or 0
        filesize = f.get("filesize") or f.get("filesize_approx") or 0
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
        format_spec = f"{format_id}+bestaudio/best"
        cmd = [
            "yt-dlp",
            "--no-warnings",
            "--quiet",
            "--progress",
            "--newline",
            "-f",
            format_spec,
            "--add-header",
            f"User-Agent:{USER_AGENT}",
            "--add-header",
            f"Referer:{url}",
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

        remaining_stderr = await process.stderr.read()
        stderr_text = remaining_stderr.decode(errors="replace")

        await process.wait()
        if process.returncode != 0:
            return False, stderr_text[:300], 0

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
        if size < 1024:
            return False, f"File too small ({size}B)", 0
        return True, "", size
    except Exception as e:
        return False, str(e)[:150], 0
