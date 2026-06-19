#!/usr/bin/env python3
# Telegram Ultimate Bot - v6
# Fixes: YouTube IP-lock via yt-dlp + 403 auto-dirpy + FFmpeg scale/rotation + size_input chat_id + pause/resume split

import asyncio
import os
import re
import sys
import logging
import time
import json
from urllib.parse import quote
from typing import Optional, Tuple, Dict

from flask import Flask
from threading import Thread

import aiohttp
import aiofiles
import base64
import gc
from aiohttp import ClientTimeout


from playwright.async_api import async_playwright

from telethon import TelegramClient, events, Button, utils
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Message,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    InputMediaUploadedDocument,
)
from FastTelethon import upload_file as fast_upload_file
from github import (
    upload_to_github,
    github_configured,
    GITHUB_MAX_MB,
    GITHUB_REPO,
    GITHUB_BRANCH,
    GITHUB_BASE_DIR,
)
from savep_handler import process_savep_request
from snapwc_handler import SnapWCSession
from y2mate import Y2MateSession

# ====================== CONFIGURATION ======================
BOT_TOKEN = "7675664254:AAGzV0-hpFhq-1jmeAB3QQwpYWKy3phYOUo"
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"

AUTHORIZED_USERS = {818185073, 6936101187, 7972834913, 8228738080}
ADMIN_ID = 818185073

MAX_FILE_SIZE_MB = 2000
OUTPUT_FOLDER = "output_files"

os.makedirs(OUTPUT_FOLDER, exist_ok=True)
HEALTH_PORT = int(os.environ.get("PORT", 10000))

video_cache: Dict[str, Dict] = {}
user_state: Dict[int, Dict] = {}
admin_pending_add: Dict[int, bool] = {}
active_downloads: Dict[str, Dict] = {}
pdfimg_sessions: Dict[str, Dict] = {}  # نگه‌داری مسیر عکس‌ها برای send all
snapwc_sessions: Dict[str, SnapWCSession] = {}  # SnapWC session references
y2mate_sessions: Dict[str, Y2MateSession] = {}  # Y2Mate session references

# آپلود گیتهاب — با /startgithub فعال، با /stopgithub غیرفعال میشه
GITHUB_ENABLED: bool = False

# نگه‌داری فایل‌های ویدیویی که کاربر فرستاده و منتظر تأیید گیتهاب هستن
video_github_pending: Dict[str, Dict] = {}

# ====================== LOGGING ======================
import sys as _sys

# ===== LOGGING: همه چیز به stdout میره تا توی Render logs دیده بشه =====
_log_formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%H:%M:%S"
)
_stdout_handler = logging.StreamHandler(_sys.stdout)
_stdout_handler.setFormatter(_log_formatter)
_stdout_handler.setLevel(logging.DEBUG)

logging.root.setLevel(logging.DEBUG)
logging.root.addHandler(_stdout_handler)

# کم‌حرف کردن کتابخونه‌های پرسروصدا
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger("UltimateBot")

# ====================== FLASK KEEP-ALIVE ======================
flask_app = Flask(__name__)


@flask_app.route("/")
def health():
    return "OK", 200


def start_keep_alive():
    Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=HEALTH_PORT, debug=False),
        daemon=True,
    ).start()


# ====================== UTILITIES ======================
def human_readable_size(num_bytes: int) -> str:
    if num_bytes == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} TB"


def safe_filename(title: str) -> str:
    return (
        re.sub(r'[<>:"/\\|?*]', "_", title.strip()[:80]) or f"file_{int(time.time())}"
    )


def parse_size_input(text: str) -> Optional[int]:
    # FIX: regex محکم‌تر — فقط عدد+واحد
    text = text.strip().lower().replace(" ", "")
    match = re.match(r"^(\d+\.?\d*)([kmg]?)b?$", text)
    if not match:
        return None
    num = float(match.group(1))
    unit = match.group(2)
    if unit == "k":
        return int(num * 1024)
    elif unit == "m":
        return int(num * 1024 * 1024)
    elif unit == "g":
        return int(num * 1024 * 1024 * 1024)
    return int(num)


async def maybe_upload_github(
    client, chat_id: int, filepath: str, file_size: int
) -> str:
    """
    اگه GITHUB_ENABLED فعال باشه فایل رو آپلود میکنه و لینک رو برمیگردونه.
    در غیر اینصورت رشته خالی برمیگردونه.
    """
    global GITHUB_ENABLED
    if not GITHUB_ENABLED:
        return ""
    if not github_configured():
        return ""
    if file_size > GITHUB_MAX_MB * 1024 * 1024:
        return ""
    try:
        gh_ok, gh_msg, gh_url = await upload_to_github(filepath)
        if gh_ok and gh_url:
            logger.info(f"GitHub upload OK: {gh_url}")
            return gh_url
        else:
            logger.warning(f"GitHub upload failed: {gh_msg}")
    except Exception as e:
        logger.warning(f"GitHub upload exception: {e}")
    return ""


async def safe_edit(msg, text: str, buttons=None):
    try:
        if buttons is not None:
            await msg.edit(text, parse_mode="markdown", buttons=buttons)
        else:
            await msg.edit(text, parse_mode="markdown")
    except Exception:
        pass


def build_progress_text(
    operation: str, current: int, total: int, speed: float, start_time: float
) -> str:
    eta = (total - current) / speed if speed > 0 else 0
    percent = (current / total) * 100 if total > 0 else 0
    filled = int(18 * current // total) if total > 0 else 0
    bar = "█" * filled + "░" * (18 - filled)
    if eta < 60:
        eta_str = f"{int(eta)}s"
    elif eta < 3600:
        eta_str = f"{int(eta // 60)}:{int(eta % 60):02d}"
    else:
        eta_str = f"{int(eta // 3600)}h{int((eta % 3600) // 60)}m"
    return (
        f"**{operation}**\n"
        f"`[{bar}]` **{percent:.1f}%**\n"
        f"📦 {human_readable_size(current)} / {human_readable_size(total)}\n"
        f"🚀 {human_readable_size(int(speed))}/s  •  ⏱ {eta_str}"
    )


# ====================== DOWNLOAD WITH PAUSE/CANCEL ======================
def _filename_from_url(
    url: str, cd_header: str = "", fallback_ext: str = "", original_url: str = ""
) -> str:
    import urllib.parse as _up
    from pathlib import Path

    name = ""
    m = re.search(r"filename\*=([^;\s]+)", cd_header)
    if m:
        raw = m.group(1).strip()
        if raw.startswith("UTF-8''"):
            name = _up.unquote(raw[7:])
        else:
            name = raw.strip('"').strip("'")
    if not name or "." not in name:
        m = re.search(r'filename="([^"]*)"', cd_header)
        if m:
            name = m.group(1).strip()
    if not name or "." not in name:
        m = re.search(r"filename=([^;\s]+)", cd_header)
        if m:
            name = m.group(1).strip()
    if not name or "." not in name:
        if original_url:
            name = Path(_up.urlparse(original_url).path).name
    if not name or "." not in name:
        name = Path(_up.urlparse(url).path).name
    if not name or "." not in name:
        name = f"file_{int(time.time())}"
    name = name.split("?")[0].split("#")[0]
    ext = os.path.splitext(name)[1]
    if not ext:
        fe = fallback_ext.lstrip(".")
        if fe:
            name = f"{name}.{fe}"
        else:
            name = f"{name}.bin"
    return name


async def download_with_controls(
    url: str,
    status_msg: Message,
    dl_id: str,
    referer: Optional[str] = None,
    extra_headers: Optional[dict] = None,
    fallback_ext: str = "",
) -> Tuple[Optional[str], Optional[str], int]:
    MAX_RETRIES = 3
    CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunks

    import urllib.parse as _up

    is_googlevideo = (
        "googlevideo.com" in url or "youtube.com" in url or "youtu.be" in url
    )
    yt_client = ""
    if is_googlevideo:
        try:
            qs = _up.parse_qs(_up.urlparse(url).query)
            yt_client = (qs.get("c", [""])[0]).upper()
        except Exception:
            pass

    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }

    if is_googlevideo:
        if yt_client == "ANDROID_VR":
            headers["User-Agent"] = (
                "com.google.android.apps.youtube.vr.oculus/1.61.48 "
                "(Linux; U; Android 12; GB) gzip"
            )
        elif yt_client == "ANDROID":
            headers["User-Agent"] = (
                "com.google.android.youtube/19.09.37 (Linux; U; Android 12) gzip"
            )
        elif yt_client == "IOS":
            headers["User-Agent"] = (
                "com.google.ios.youtube/19.09.3 (iPhone14,3; U; CPU iOS 15_6 like Mac OS X)"
            )
        else:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
        # googlevideo URLs reject requests with Referer/Origin
    else:
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        if referer:
            headers["Referer"] = referer
            try:
                headers["Origin"] = "/".join(referer.split("/")[:3])
            except Exception:
                pass

    if extra_headers:
        headers.update(extra_headers)
    if is_googlevideo and "Range" not in headers:
        headers["Range"] = "bytes=0-"

    timeout = ClientTimeout(total=None, connect=30, sock_read=120)
    original_name = _filename_from_url(url, "", fallback_ext)
    filepath = os.path.join(OUTPUT_FOLDER, f"dl_{int(time.time())}_{original_name}")
    downloaded = 0
    total = 0
    last_update = 0.0
    last_bytes_for_speed = 0
    last_time_for_speed = time.time()
    start_time = time.time()

    if dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}

    dl_buttons_pause = [
        [
            Button.inline("⏸ Pause", f"dlpause_{dl_id}"),
            Button.inline("❌ Cancel", f"dlcancel_{dl_id}"),
        ]
    ]
    dl_buttons_resume = [
        [
            Button.inline("▶️ Resume", f"dlresume_{dl_id}"),
            Button.inline("❌ Cancel", f"dlcancel_{dl_id}"),
        ]
    ]

    logger.info(f"[DL] START | url={url[:120]}")
    await safe_edit(status_msg, "📥 Connecting...", buttons=dl_buttons_pause)

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info(
            f"[DL] Attempt {attempt}/{MAX_RETRIES} | downloaded_so_far={human_readable_size(downloaded)}"
        )
        try:
            attempt_headers = headers.copy()
            if downloaded > 0:
                attempt_headers["Range"] = f"bytes={downloaded}-"
                await safe_edit(
                    status_msg,
                    f"🔄 Retry {attempt}/{MAX_RETRIES} — resuming from {human_readable_size(downloaded)}...",
                    buttons=dl_buttons_pause,
                )

            connector = aiohttp.TCPConnector(limit=8, ttl_dns_cache=300, ssl=False)
            async with aiohttp.ClientSession(
                timeout=timeout, connector=connector
            ) as session:
                async with session.get(
                    url, headers=attempt_headers, allow_redirects=True
                ) as response:
                    # FIX: 403 رو به عنوان کد خاص برمیگردونه تا caller تصمیم بگیره
                    if response.status == 403:
                        return None, "HTTP_403", 0
                    if response.status not in (200, 206):
                        return None, f"HTTP {response.status}", 0

                    if total == 0:
                        content_length = int(response.headers.get("content-length", 0))
                        if response.status == 206:
                            cr = response.headers.get("content-range", "")
                            m = re.search(r"/(\d+)", cr)
                            total = (
                                int(m.group(1)) if m else content_length + downloaded
                            )
                        else:
                            total = content_length
                        if total > MAX_FILE_SIZE_MB * 1024 * 1024:
                            return (
                                None,
                                f"File too large ({human_readable_size(total)})",
                                0,
                            )
                        cd = response.headers.get("Content-Disposition", "")
                        cd_name = _filename_from_url(
                            str(response.url), cd, fallback_ext
                        )
                        filepath = os.path.join(
                            OUTPUT_FOLDER, f"dl_{int(time.time())}_{cd_name}"
                        )

                    if response.status == 200 and downloaded > 0:
                        downloaded = 0
                        write_mode = "wb"
                    elif response.status == 206 and downloaded > 0:
                        write_mode = "ab"
                    else:
                        write_mode = "wb"
                    async with aiofiles.open(filepath, write_mode) as f:
                        async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                            if active_downloads.get(dl_id, {}).get("cancelled"):
                                try:
                                    if os.path.exists(filepath):
                                        os.remove(filepath)
                                except Exception:
                                    pass
                                try:
                                    await status_msg.edit(
                                        "🚫 Download cancelled.", buttons=None
                                    )
                                except Exception:
                                    pass
                                return None, "Cancelled by user", 0

                            if active_downloads.get(dl_id, {}).get("paused"):
                                paused_text = build_progress_text(
                                    "⏸ Paused", downloaded, total, 0, start_time
                                )
                                await safe_edit(
                                    status_msg, paused_text, buttons=dl_buttons_resume
                                )
                                while active_downloads.get(dl_id, {}).get("paused"):
                                    if active_downloads.get(dl_id, {}).get("cancelled"):
                                        try:
                                            if os.path.exists(filepath):
                                                os.remove(filepath)
                                        except Exception:
                                            pass
                                        try:
                                            await status_msg.edit(
                                                "🚫 Download cancelled.", buttons=None
                                            )
                                        except Exception:
                                            pass
                                        return None, "Cancelled by user", 0
                                    await asyncio.sleep(0.5)
                                last_update = 0.0

                            await f.write(chunk)
                            downloaded += len(chunk)

                            now = time.time()
                            if now - last_update >= 1.5 and downloaded != total:
                                dt = now - last_time_for_speed
                                speed = (
                                    (downloaded - last_bytes_for_speed) / dt
                                    if dt > 0
                                    else 0
                                )
                                last_bytes_for_speed = downloaded
                                last_time_for_speed = now
                                last_update = now
                                text = build_progress_text(
                                    "📥 Downloading",
                                    downloaded,
                                    total,
                                    speed,
                                    start_time,
                                )
                                await safe_edit(
                                    status_msg, text, buttons=dl_buttons_pause
                                )

            active_downloads.pop(dl_id, None)
            logger.info(
                f"[DL] DONE | size={human_readable_size(downloaded)} | file={filepath}"
            )
            try:
                await status_msg.edit(
                    "✅ Download complete!", parse_mode="markdown", buttons=None
                )
            except Exception:
                pass
            return filepath, None, downloaded

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            aiohttp.ServerDisconnectedError,
        ) as e:
            logger.warning(f"Download attempt {attempt} failed: {e}")
            if attempt == MAX_RETRIES:
                active_downloads.pop(dl_id, None)
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except Exception:
                    pass
                return None, f"Failed after {MAX_RETRIES} retries: {str(e)[:80]}", 0
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Unexpected download error: {e}")
            active_downloads.pop(dl_id, None)
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
            except Exception:
                pass
            return None, str(e)[:100], 0

    active_downloads.pop(dl_id, None)
    return None, "Download failed", 0


# ====================== YT-DLP YOUTUBE DOWNLOAD ======================
def _is_youtube_source(url: str) -> bool:
    """تشخیص اینکه لینک مربوط به صفحه یوتیوب هست یا نه (نه googlevideo)."""
    u = url.lower()
    return (
        "youtube.com/watch" in u
        or "youtu.be/" in u
        or "youtube.com/shorts" in u
        or "m.youtube.com" in u
    )


async def download_youtube_ytdlp(
    youtube_url: str,
    status_msg: Message,
    dl_id: str,
) -> Tuple[Optional[str], Optional[str], int]:
    """
    دانلود مستقیم یوتیوب با yt-dlp روی خود سرور.
    این کار مشکل IP-lock و User-Agent mismatch رو کامل حل می‌کنه چون
    لینک نهایی googlevideo با IP خود سرور ساخته می‌شه.
    """
    output_tmpl = os.path.join(OUTPUT_FOLDER, f"yt_{int(time.time())}.%(ext)s")

    if dl_id not in active_downloads:
        active_downloads[dl_id] = {"paused": False, "cancelled": False}

    dl_buttons_cancel = [[Button.inline("❌ Cancel", f"dlcancel_{dl_id}")]]

    await safe_edit(
        status_msg,
        "📥 Downloading via yt-dlp (server-side)...",
        buttons=dl_buttons_cancel,
    )
    logger.info(f"[YTDLP] START | url={youtube_url[:120]}")

    # فرمت: بهترین کیفیت mp4 ترکیبی، یا بهترین video+audio با merge
    args = [
        "yt-dlp",
        "-f",
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "--no-playlist",
        "--no-warnings",
        "--newline",
        "--progress",
        "-o",
        output_tmpl,
        youtube_url,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        last_update = 0.0
        # خواندن خط‌به‌خط خروجی برای نمایش پیشرفت و چک کردن cancel
        while True:
            if active_downloads.get(dl_id, {}).get("cancelled"):
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await status_msg.edit("🚫 Download cancelled.", buttons=None)
                except Exception:
                    pass
                return None, "Cancelled by user", 0

            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                if proc.returncode is not None:
                    break
                continue

            if not line:
                break

            text = line.decode(errors="replace").strip()
            # خط‌های پیشرفت yt-dlp مثل: [download]  45.3% of 12.34MiB at 1.23MiB/s
            m = re.search(
                r"\[download\]\s+([\d.]+)%.*?of\s+([\d.]+\w+).*?at\s+([\d.]+\w+/s)",
                text,
            )
            if m:
                now = time.time()
                if now - last_update >= 2.0:
                    last_update = now
                    percent = m.group(1)
                    size = m.group(2)
                    speed = m.group(3)
                    await safe_edit(
                        status_msg,
                        f"📥 **Downloading (yt-dlp)**\n"
                        f"📊 {percent}%  •  📦 {size}  •  🚀 {speed}",
                        buttons=dl_buttons_cancel,
                    )

        await proc.wait()
        active_downloads.pop(dl_id, None)

        if proc.returncode != 0:
            logger.warning(f"[YTDLP] failed | rc={proc.returncode}")
            return None, "YTDLP_FAILED", 0

        # پیدا کردن فایل خروجی
        found = None
        prefix = os.path.basename(output_tmpl).split("%")[0]
        for fname in os.listdir(OUTPUT_FOLDER):
            if fname.startswith(prefix):
                found = os.path.join(OUTPUT_FOLDER, fname)
                break

        if not found or not os.path.exists(found) or os.path.getsize(found) == 0:
            return None, "YTDLP_FAILED", 0

        size = os.path.getsize(found)
        logger.info(f"[YTDLP] DONE | size={human_readable_size(size)} | file={found}")
        try:
            await status_msg.edit("✅ Download complete!", buttons=None)
        except Exception:
            pass
        return found, None, size

    except FileNotFoundError:
        active_downloads.pop(dl_id, None)
        return None, "yt-dlp not installed on server", 0
    except Exception as e:
        active_downloads.pop(dl_id, None)
        logger.error(f"[YTDLP] error: {e}")
        return None, str(e)[:100], 0


async def get_youtube_meta_aiohttp(url: str) -> dict:
    """Fetch YouTube title (oEmbed) + description (meta tag) with simple HTTP requests."""
    result = {"title": "", "description": ""}
    try:
        async with aiohttp.ClientSession() as s:
            oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
            async with s.get(oembed_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    data = await r.json()
                    result["title"] = data.get("title", "")
    except Exception:
        pass
    try:
        import re as _re

        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    html = await r.text()
                    m = _re.search(
                        r'<meta\s+name="description"\s+content="([^"]*)"',
                        html,
                        _re.IGNORECASE,
                    )
                    if m:
                        result["description"] = m.group(1)
    except Exception:
        pass
    return result


# ====================== STREAMING DOWNLOAD (FFMPEG) ======================


def is_stream_url(url: str) -> bool:
    keywords = ["m3u8", "mp4", "play?", "stream", "video"]
    return any(k in url.lower() for k in keywords)


async def extract_m3u8_from_html(page_url: str) -> str | None:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
        }
        async with aiohttp.ClientSession() as s:
            async with s.get(
                page_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status != 200:
                    return None
                html = await r.text()
                import re as _re

                for m in _re.finditer(r'https?://[^"\']*\.m3u8[^"\'\s]*', html):
                    return m.group(0)
                for m in _re.finditer(r'https?://[^"\']*\.mp4[^"\'\s]*', html):
                    return m.group(0)
    except Exception:
        pass
    return None


async def download_stream_ffmpeg(
    url: str, filepath: str, referer: str = ""
) -> tuple[bool, str]:
    cmd = ["ffmpeg", "-y"]
    if referer:
        cmd.extend(
            [
                "-headers",
                f"Referer: {referer}\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\nAccept: */*\r\n",
            ]
        )
    cmd.extend(["-i", url, "-c", "copy", filepath])
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode == 0:
            return True, ""
        err_text = (
            stderr.decode(errors="replace")[-300:]
            if stderr
            else f"ffmpeg exit code {proc.returncode}"
        )
        return False, err_text
    except asyncio.TimeoutError:
        return False, "ffmpeg timed out (600s)"
    except FileNotFoundError:
        return False, "ffmpeg not found on server"
    except Exception as e:
        return False, str(e)[:100]


async def try_stream_download(
    url: str, status_msg, referer: str = ""
) -> tuple[str | None, str | None]:
    """Try ffmpeg stream download, then m3u8 extraction from page."""
    stream_path = os.path.join(OUTPUT_FOLDER, f"stream_{int(time.time())}.mp4")
    await safe_edit(status_msg, "🎬 Detected stream — trying ffmpeg...")
    ok, err_msg = await download_stream_ffmpeg(url, stream_path, referer=referer)
    if ok and os.path.exists(stream_path) and os.path.getsize(stream_path) > 1024:
        return stream_path, None
    await safe_edit(status_msg, "🔍 Trying to extract streaming URL from page...")
    m3u8 = await extract_m3u8_from_html(url)
    if m3u8:
        stream_path2 = os.path.join(OUTPUT_FOLDER, f"stream_{int(time.time())}.mp4")
        ok2, err_msg2 = await download_stream_ffmpeg(
            m3u8, stream_path2, referer=referer
        )
        if (
            ok2
            and os.path.exists(stream_path2)
            and os.path.getsize(stream_path2) > 1024
        ):
            return stream_path2, None
        err_msg = err_msg2
    if os.path.exists(stream_path):
        try:
            os.remove(stream_path)
        except Exception:
            pass
    return None, err_msg or "Stream download failed"


async def get_youtube_meta_seostudio(url: str) -> dict:
    """Extract YouTube title + description from seostudio.tools using Playwright."""
    result = {"title": "", "description": ""}
    import random

    user_agent = random.choice(
        [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
        ]
    )
    logger.info("[SEOSTUDIO] Launching browser...")
    try:
        async with async_playwright() as pw:
            logger.info("[SEOSTUDIO] Playwright started")
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-web-security",
                    "--disable-infobars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-dev-shm-usage",
                    "--single-process",
                    "--window-size=1280,800",
                ],
            )
            logger.info("[SEOSTUDIO] Browser launched")
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=user_agent,
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = await ctx.new_page()

            for retry in range(3):
                try:
                    logger.info(f"[SEOSTUDIO] Page goto attempt {retry + 1}/3...")
                    await page.goto(
                        "https://seostudio.tools/youtube-description-extractor",
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    logger.info("[SEOSTUDIO] Page loaded")
                    break
                except Exception as e:
                    logger.warning(
                        f"[SEOSTUDIO] Goto failed (attempt {retry + 1}): {e}"
                    )
                    if retry < 2:
                        await asyncio.sleep(3)
                    else:
                        raise

            await asyncio.sleep(3)
            logger.info("[SEOSTUDIO] Waiting for input field...")
            url_input = page.locator("#input")
            await url_input.wait_for(timeout=30000)
            await url_input.click()
            await url_input.fill("")
            logger.info("[SEOSTUDIO] URL typed, clicking extract...")
            await url_input.type(url, delay=30)

            extract_btn = page.locator(
                'span[wire\\:target="onYoutubeDescriptionExtractor"]'
            )
            await extract_btn.wait_for(timeout=10000)
            await extract_btn.click()
            logger.info("[SEOSTUDIO] Extract clicked, waiting for result...")

            textarea = page.locator("#text")
            await textarea.wait_for(timeout=20000)
            await asyncio.sleep(2)

            content = await textarea.input_value()
            logger.info(
                f"[SEOSTUDIO] Got content (len={len(content) if content else 0})"
            )
            if content:
                lines = content.strip().split("\n", 1)
                result["title"] = lines[0].strip()
                result["description"] = lines[1].strip() if len(lines) > 1 else ""
                logger.info(f"[SEOSTUDIO] Title: {result['title'][:80]}")
    except Exception as e:
        logger.warning(f"[SEOSTUDIO] Error: {e}", exc_info=True)
    return result


# ====================== PAUSE / RESUME / CANCEL CALLBACKS ======================
# FIX: pause و resume دو callback جدا دارن — قبلاً toggle بود که race condition داشت


async def dl_pause_callback(event):
    dl_id = event.data.decode().replace("dlpause_", "")
    if dl_id not in active_downloads:
        return await event.answer("No active download found.", alert=True)
    active_downloads[dl_id]["paused"] = True
    await event.answer("⏸ Paused!", alert=False)


async def dl_resume_callback(event):
    dl_id = event.data.decode().replace("dlresume_", "")
    if dl_id not in active_downloads:
        return await event.answer("No active download found.", alert=True)
    active_downloads[dl_id]["paused"] = False
    await event.answer("▶️ Resumed!", alert=False)


async def dl_cancel_callback(event):
    dl_id = event.data.decode().replace("dlcancel_", "")
    if dl_id not in active_downloads:
        return await event.answer("No active download found.", alert=True)
    active_downloads[dl_id]["cancelled"] = True
    active_downloads[dl_id]["paused"] = False
    await event.answer("❌ Cancelling...", alert=False)
    try:
        await event.edit(buttons=None)
    except Exception:
        pass


# ====================== UPLOAD WITH PROGRESS ======================
async def get_video_thumbnail(filepath: str) -> Optional[str]:
    """یه فریم از وسط ویدیو به عنوان thumbnail می‌گیره"""
    try:
        thumb_path = filepath + "_thumb.jpg"
        # مدت ویدیو رو بگیر تا فریم از وسط باشه
        probe = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            filepath,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await probe.communicate()
        duration = 0.0
        try:
            duration = float(
                json.loads(stdout.decode()).get("format", {}).get("duration", 0)
            )
        except Exception:
            pass
        seek_time = max(duration / 2, 1) if duration > 2 else 0

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-ss",
            str(seek_time),
            "-i",
            filepath,
            "-vframes",
            "1",
            "-q:v",
            "2",
            "-vf",
            "scale=320:-1",
            thumb_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception:
        pass
    return None


async def send_file_with_progress(
    client,
    chat_id: int,
    filepath: str,
    caption: str,
    status_msg: Message,
    buttons=None,
    supports_streaming: bool = True,
):
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        await safe_edit(status_msg, "❌ File is empty or missing.")
        return None
    file_size = os.path.getsize(filepath)
    start_time = time.time()
    last_update = [0.0]
    last_bytes = [0]
    last_time = [start_time]

    duration, width, height = await get_video_info(filepath)
    is_audio = (duration and duration > 0 and width == 0 and height == 0) or (
        os.path.splitext(filepath)[1].lower()
        in (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac", ".opus")
    )
    thumb_path = await get_video_thumbnail(filepath) if not is_audio else None

    async def progress_cb(current: int, total: int):
        now = time.time()
        if now - last_update[0] < 3.0 and current != total:
            return
        last_update[0] = now
        dt = now - last_time[0]
        speed = (current - last_bytes[0]) / dt if dt > 0 else 0
        last_bytes[0] = current
        last_time[0] = now
        text = build_progress_text("📤 Uploading", current, total, speed, start_time)
        try:
            asyncio.ensure_future(status_msg.edit(text, parse_mode="markdown"))
        except Exception:
            pass

    try:
        duration_int = int(duration) if duration else 0
        with open(filepath, "rb") as f:
            uploaded = await fast_upload_file(
                client, f, progress_callback=progress_cb, connection_count=15
            )

        if is_audio:
            attributes, mime_type = utils.get_attributes(
                filepath,
                attributes=[
                    DocumentAttributeAudio(duration=duration_int, title="Audio")
                ],
            )
        else:
            attributes, mime_type = utils.get_attributes(
                filepath,
                attributes=[
                    DocumentAttributeVideo(
                        duration=duration_int,
                        w=width if width else 0,
                        h=height if height else 0,
                        supports_streaming=True,
                    )
                ],
            )
        thumb_input = None
        if not is_audio and thumb_path and os.path.exists(thumb_path):
            with open(thumb_path, "rb") as tf:
                thumb_input = await fast_upload_file(client, tf)

        media = InputMediaUploadedDocument(
            file=uploaded,
            mime_type=mime_type,
            attributes=attributes,
            thumb=thumb_input,
            force_file=False,
        )
        sent = await client.send_file(
            chat_id,
            media,
            caption=caption,
            buttons=buttons,
            parse_mode="markdown",
        )
        return sent
    except Exception as e:
        err_str = str(e)
        if "file parts" in err_str.lower():
            await safe_edit(
                status_msg, "⚠️ Fast upload failed, retrying with direct send..."
            )
            try:
                sent = await client.send_file(
                    chat_id,
                    filepath,
                    caption=caption,
                    buttons=buttons,
                    parse_mode="markdown",
                    supports_streaming=supports_streaming,
                )
                return sent
            except Exception as e2:
                await safe_edit(status_msg, f"❌ Upload failed: {str(e2)[:100]}")
                return None
        else:
            await safe_edit(status_msg, f"❌ Upload failed: {err_str[:100]}")
            return None
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass
        try:
            await status_msg.delete()
        except Exception:
            pass


# ====================== DOWNLOAD AND SEND ======================
async def do_download_and_send(
    event,
    status_msg,
    direct_url: str,
    source_url: str,
    extra_headers: Optional[dict] = None,
    title: str = "",
    description: str = "",
    skip_ytdlp: bool = False,
    fallback_ext: str = "",
) -> bool:
    dl_id = f"dl_{event.chat_id}_{event.id}_{int(time.time())}"
    active_downloads[dl_id] = {"paused": False, "cancelled": False}

    filepath = None
    dl_error = None
    final_size = 0

    # ===== اگه لینک یوتیوب هست و skip_ytdlp فعال نیست، اول با yt-dlp =====
    if not skip_ytdlp and (
        _is_youtube_source(source_url) or _is_youtube_source(direct_url)
    ):
        yt_target = source_url if _is_youtube_source(source_url) else direct_url
        filepath, dl_error, final_size = await download_youtube_ytdlp(
            yt_target, status_msg, dl_id
        )
        # اگه yt-dlp شکست خورد، با روش معمولی ادامه بده
        if dl_error == "YTDLP_FAILED" or (
            not filepath and dl_error != "Cancelled by user"
        ):
            await safe_edit(status_msg, "🔄 yt-dlp failed — trying direct download...")
            filepath = None
            dl_error = None
            final_size = 0

    # ===== دانلود معمولی (اگه یوتیوب نبود یا yt-dlp شکست خورد) =====
    if not filepath and dl_error != "Cancelled by user":
        dl_id_direct = f"dl_{event.chat_id}_{event.id}_{int(time.time())}_d"
        active_downloads[dl_id_direct] = {"paused": False, "cancelled": False}
        filepath, dl_error, final_size = await download_with_controls(
            direct_url,
            status_msg,
            dl_id_direct,
            referer=source_url,
            extra_headers=extra_headers,
            fallback_ext=fallback_ext,
        )

        # FIX: 403 → auto-retry via dirpy (فقط برای لینک صفحه ویدیو، نه فایل مستقیم)
        if dl_error == "HTTP_403":
            from urllib.parse import urlparse as _up
            import os as _os

            _ext = _os.path.splitext(_up(source_url).path)[1]
            if _ext:
                await safe_edit(
                    status_msg, f"❌ Server returned 403 (blocked) for this file."
                )
                return False
            await safe_edit(status_msg, "🔄 403 received — extracting via Dirpy...")
            (
                found_urls,
                session_headers,
                intercept_err,
                page_title,
            ) = await extract_video_url_smart(source_url, status_msg)
            if not found_urls:
                await safe_edit(
                    status_msg,
                    f"❌ Could not extract via Dirpy either:\n{intercept_err}",
                )
                return False
            if page_title and not title:
                title = page_title
            direct_url = found_urls[0]
            extra_headers = session_headers
            dl_id2 = f"dl_{event.chat_id}_{event.id}_{int(time.time())}_r"
            active_downloads[dl_id2] = {"paused": False, "cancelled": False}
            filepath, dl_error, final_size = await download_with_controls(
                direct_url,
                status_msg,
                dl_id2,
                referer=source_url,
                extra_headers=extra_headers,
                fallback_ext=fallback_ext,
            )

    if dl_error or not filepath:
        if dl_error != "Cancelled by user" and is_stream_url(direct_url):
            stream_fp, stream_err = await try_stream_download(
                direct_url, status_msg, referer=source_url
            )
            if stream_fp:
                filepath, dl_error = stream_fp, None
                final_size = os.path.getsize(stream_fp)
            else:
                await safe_edit(status_msg, f"❌ Download failed: {stream_err}")
                return False
        elif dl_error != "Cancelled by user":
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
            return False
        else:
            return False

    import os as _os_audio

    vid_duration, vw, vh = await get_video_info(filepath)

    _audio_exts = (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac", ".opus")
    _is_audio = (vid_duration and vid_duration > 0 and vw == 0 and vh == 0) or (
        (not vid_duration or vid_duration <= 0)
        and _os_audio.path.splitext(filepath)[1].lower() in _audio_exts
    )

    sent_msg = None

    if _is_audio:
        await safe_edit(status_msg, "🎵 Uploading audio...")
        try:
            _vd = vid_duration or 0
            sent_msg = await event.client.send_file(
                event.chat_id,
                filepath,
                caption="🎵 Audio",
                attributes=[DocumentAttributeAudio(duration=int(_vd), title="Audio")],
                supports_streaming=True,
            )
        except Exception as e:
            logger.error(f"Audio upload error: {e}")
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
            try:
                os.remove(filepath)
            except Exception:
                pass
            return False
    elif vid_duration is None or vid_duration <= 0:
        fsize = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        basename = os.path.basename(filepath)
        await safe_edit(status_msg, "📤 Uploading file...")
        try:
            sent_msg = await event.client.send_file(
                event.chat_id,
                filepath,
                caption=f"📎 **{basename}**",
                force_document=True,
            )
        except Exception as e:
            logger.error(f"File upload error: {e}")
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
            try:
                os.remove(filepath)
            except Exception:
                pass
            return False
    else:
        await safe_edit(status_msg, "📤 Uploading...")
        try:
            dur_str = ""
            if vid_duration and vid_duration > 0:
                mins, secs = divmod(int(vid_duration), 60)
                hours, mins = divmod(mins, 60)
                if hours > 0:
                    dur_str = f"\n⏱ Duration: {hours}:{mins:02d}:{secs:02d}"
                else:
                    dur_str = f"\n⏱ Duration: {mins}:{secs:02d}"

            gh_line = ""
            if GITHUB_ENABLED:
                await safe_edit(status_msg, "☁️ Uploading to GitHub...")
                gh_url = await maybe_upload_github(
                    event.client, event.chat_id, filepath, final_size
                )
                if gh_url:
                    gh_line = f"\n☁️ [GitHub DL]({gh_url})"
            sent_msg = await send_file_with_progress(
                client=event.client,
                chat_id=event.chat_id,
                filepath=filepath,
                caption=(
                    f"🎬 **Video Downloaded**\n"
                    f"📦 Size: {human_readable_size(final_size)}"
                    f"{dur_str}\n"
                    f"🔗 [Source]({source_url})"
                    f"\n⬇️ [DW Link]({direct_url})"
                    f"{gh_line}"
                ),
                status_msg=status_msg,
                supports_streaming=True,
            )
        except Exception as e:
            logger.error(f"Upload error: {e}")
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
            try:
                os.remove(filepath)
            except Exception:
                pass
            return False

    # ===== بعد از آپلود: گرفتن تایتل و دیسکریپشن از seostudio =====
    seo_title = ""
    seo_desc = ""
    if _is_youtube_source(source_url):
        await safe_edit(status_msg, "📝 Fetching title & description...")
        try:
            seo_meta = await asyncio.wait_for(
                get_youtube_meta_seostudio(source_url), timeout=90
            )
            seo_title = seo_meta.get("title", "")
            seo_desc = seo_meta.get("description", "")
        except asyncio.TimeoutError:
            await event.client.send_message(
                event.chat_id,
                "⏱ Seostudio timed out (90s) — title/description not available",
            )
        except Exception as e:
            await event.client.send_message(
                event.chat_id, f"❌ Seostudio error: {str(e)[:200]}"
            )

    if sent_msg and (seo_title or seo_desc):
        info_parts = []
        if seo_title:
            info_parts.append(f"**{seo_title}**")
            try:
                new_caption = f"🎬 {seo_title}"
                if _is_audio:
                    new_caption = f"🎵 {seo_title}"
                if seo_desc:
                    new_caption += f"\n\n📝 {seo_desc}"
                await sent_msg.edit(caption=new_caption)
            except Exception:
                pass
        if seo_desc:
            info_parts.append(seo_desc)
        info_text = "\n\n".join(info_parts)
        if info_text:
            try:
                await event.client.send_message(
                    event.chat_id, info_text, parse_mode="markdown"
                )
            except Exception:
                pass

    try:
        os.remove(filepath)
    except Exception:
        pass
    return True


# ====================== GET FILE SIZE ======================
async def get_file_size(url: str) -> int:
    try:
        timeout = ClientTimeout(connect=10, sock_read=10, total=15)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(
                url, headers=headers, allow_redirects=True, ssl=False
            ) as resp:
                return int(resp.headers.get("content-length", 0))
    except Exception:
        return 0


def _url_label(url: str, size: int, index: int) -> str:
    u = url.lower()
    quality = "Unknown"
    for q in ["2160p", "1080p", "720p", "480p", "360p", "240p", "4k", "hd", "sd"]:
        if q in u:
            quality = q.upper()
            break
    sz_str = human_readable_size(size) if size > 0 else "? MB"
    try:
        from urllib.parse import urlparse

        domain = urlparse(url).netloc.replace("www.", "")[:20]
    except Exception:
        domain = f"Link {index + 1}"
    return f"#{index + 1} {quality} • {sz_str} • {domain}"


# ====================== VIDEO URL EXTRACTOR ======================
SKIP_KEYWORDS = [
    "thumb",
    "preview",
    "poster",
    "banner",
    "logo",
    "icon",
    "sprite",
    "storyboard",
    "tracking",
    "analytics",
    "pixel",
    "ad/",
    "/ads/",
]
MIN_SIZE = 2 * 1024 * 1024  # 2MB


def _browser_args() -> list:
    """آرگومان‌های chromium برای مصرف RAM کم."""
    return [
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--hide-scrollbars",
        "--mute-audio",
        "--no-first-run",
        "--js-flags=--max-old-space-size=96",
    ]


KNOWN_CDN_DOMAINS = [
    "rdtcdn.com",
    "phncdn.com",
    "xnxx-cdn.com",
    "media4.luxuretv",
    "media.luxuretv",
    "rule34.xxx",
    "rule34video",
    "kv-ph.",
    "ev-ph.",
    "di-ph.",
    "googlevideo.com",
    "videoplayback",
    "p300cdn",
    "x-tg.tube/get_file",
]


def _should_capture(url: str, content_type: str = "", content_length: int = 0) -> bool:
    ul = url.lower()
    if any(k in ul for k in SKIP_KEYWORDS):
        return False
    if "video/" in content_type and content_length > MIN_SIZE:
        return True
    is_known_cdn = any(d in ul for d in KNOWN_CDN_DOMAINS)
    has_video_ext = (
        ".mp4" in ul or ".webm" in ul or "videoplayback" in ul or "/get_file/" in ul
    )
    if is_known_cdn and has_video_ext:
        if "rdtcdn.com" in ul or "phncdn.com" in ul:
            quality_signals = [
                "_720p_",
                "_1080p_",
                "_480p_",
                "_240p_",
                "_2160p_",
                "_4000k_",
                "_2000k_",
                "_1000k_",
                "_500k_",
                "_800k_",
                "p_720",
                "p_1080",
                "p_480",
                "p_240",
            ]
            return any(q in ul for q in quality_signals)
        return True
    return False


def _extract_from_html(html: str, seen: set, captured_urls: list, label: str):
    for m in re.findall(r"https?://[^\x22\x27<>\s]+", html):
        if _should_capture(m):
            norm = m.split("?")[0]
            if norm not in seen:
                seen.add(norm)
                captured_urls.append(m)
                logger.info(f"[{label}-URL] {m[:180]}")

    kv_patterns = [
        r"video_url\s*:\s*[\x22\x27](?:function/\d+/)?(https?://[^\x22\x27\s]+)[\x22\x27]",
        r"video_url_hd\s*:\s*[\x22\x27](?:function/\d+/)?(https?://[^\x22\x27\s]+)[\x22\x27]",
        r"event_reporting2\s*:\s*[\x22\x27]([^\x22\x27\s]+/get_file/[^\x22\x27\s]+)[\x22\x27]",
        r"[\x22\x27](?:file|src|url|video_url)[\x22\x27\s]*:\s*[\x22\x27](?:function/\d+/)?(https?://[^\x22\x27\s]+\.mp4[^\x22\x27\s]*)[\x22\x27]",
    ]
    for pat in kv_patterns:
        for m in re.findall(pat, html, re.IGNORECASE):
            url = m.rstrip("/")
            if not url.startswith("http"):
                continue
            norm = url.split("?")[0]
            if norm not in seen and not any(k in url.lower() for k in SKIP_KEYWORDS):
                seen.add(norm)
                captured_urls.append(url)
                logger.info(f"[{label}-KV] {url[:180]}")

    for m in re.findall(
        r"[\x22\x27]([^\x22\x27]*?/get_file/[^\x22\x27]+\.mp4[^\x22\x27]*)[\x22\x27]",
        html,
    ):
        if m.startswith("http"):
            url = m.rstrip("/")
            norm = url.split("?")[0]
            if norm not in seen:
                seen.add(norm)
                captured_urls.append(url)
                logger.info(f"[{label}-GETFILE] {url[:180]}")


async def _collect_from_page(page, label: str, captured_urls: list, seen: set):
    async def on_response(response):
        try:
            ct = response.headers.get("content-type", "")
            cl = int(response.headers.get("content-length", 0))
            ru = response.url
            if _should_capture(ru, ct, cl):
                norm = ru.split("?")[0]
                if norm not in seen:
                    seen.add(norm)
                    captured_urls.append(ru)
                    logger.info(f"[{label}] {ru[:180]}")
        except Exception:
            pass

    page.on("response", on_response)

    try:
        html = await page.content()
        _extract_from_html(html, seen, captured_urls, label + "-FAST")
    except Exception:
        pass

    if not captured_urls:
        await page.wait_for_timeout(5000)
        try:
            html = await page.content()
            _extract_from_html(html, seen, captured_urls, label + "-AFTER5S")
        except Exception:
            pass

    if not captured_urls:
        await page.evaluate(
            '() => { try { document.querySelector("video")?.play(); } catch(e){} }'
        )
        await page.wait_for_timeout(6000)
        try:
            html = await page.content()
            _extract_from_html(html, seen, captured_urls, label + "-AFTERPLAY")
        except Exception:
            pass


async def extract_video_url_smart(
    video_url: str, status_msg: Message
) -> Tuple[list, dict, Optional[str], str]:
    async with async_playwright() as p:
        browser = None
        captured_urls: list = []
        seen: set = set()
        session_headers: dict = {}
        video_title: str = ""

        try:
            browser = await p.chromium.launch(headless=True, args=_browser_args())
            logger.info(f"[PLAYWRIGHT] Browser launched")

            async def make_context():
                return await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 720},
                )

            # مرحله ۱: Dirpy
            await safe_edit(status_msg, "🔗 Opening Dirpy Studio...")
            ctx1 = await make_context()
            page1 = await ctx1.new_page()
            dirpy_url = f"https://dirpy.com/studio?url={quote(video_url)}"
            try:
                logger.info(f"[PLAYWRIGHT] Opening Dirpy: {dirpy_url[:120]}")
                await page1.goto(
                    dirpy_url, wait_until="domcontentloaded", timeout=60000
                )
                await _collect_from_page(page1, "DIRPY", captured_urls, seen)
                try:
                    raw = await page1.title()
                    video_title = raw.replace("Dirpy Studio", "").strip(" -|").strip()
                except:
                    pass
                if captured_urls:
                    session_headers = {"Referer": video_url}
            except Exception as e:
                logger.warning(f"Dirpy page error: {e}")
            finally:
                await page1.close()
                await ctx1.close()

            # مرحله ۲: Direct site fallback
            if not captured_urls:
                await safe_edit(
                    status_msg, "🌐 Dirpy failed — trying direct site extraction..."
                )
                ctx2 = await make_context()
                page2 = await ctx2.new_page()
                try:
                    logger.info(f"[PLAYWRIGHT] Direct goto: {video_url[:120]}")

                    async def handle_dialog(dialog):
                        await dialog.accept()

                    page2.on("dialog", handle_dialog)

                    await page2.goto(
                        video_url, wait_until="domcontentloaded", timeout=60000
                    )

                    age_selectors = [
                        'button:has-text("I AM 18")',
                        'button:has-text("ENTER")',
                        'button:has-text("Yes")',
                        ".age-gate button",
                        "button.y",
                        'button:has-text("Enter")',
                        'button:has-text("Confirm")',
                        'a:has-text("I AM 18")',
                        'a:has-text("ENTER")',
                    ]
                    for sel in age_selectors:
                        try:
                            el = page2.locator(sel).first
                            if await el.is_visible(timeout=800):
                                await el.click()
                                await asyncio.sleep(1.5)
                                break
                        except Exception:
                            continue

                    await _collect_from_page(page2, "DIRECT", captured_urls, seen)

                    if captured_urls:
                        raw_cookies = await ctx2.cookies()
                        cookie_str = "; ".join(
                            f"{c['name']}={c['value']}"
                            for c in raw_cookies
                            if video_url.split("/")[2].replace("www.", "")
                            in c.get("domain", "")
                            or c.get("domain", "").lstrip(".") in video_url
                        )
                        session_headers = {
                            "Referer": video_url,
                            "Origin": "/".join(video_url.split("/")[:3]),
                        }
                        if cookie_str:
                            session_headers["Cookie"] = cookie_str

                except Exception as e:
                    logger.warning(f"Direct page error: {e}")
                finally:
                    await page2.close()
                    await ctx2.close()

            if captured_urls:
                return captured_urls, session_headers, None, video_title
            return (
                [],
                {},
                "Could not capture video link via Dirpy or direct extraction",
                video_title,
            )

        except Exception as e:
            logger.error(f"Extractor error: {e}")
            return [], {}, str(e), ""
        finally:
            if browser:
                await browser.close()


# ====================== HTML TO PDF ======================
async def html_to_pdf(
    url: str, status_msg: Message
) -> Tuple[Optional[str], Optional[str], int]:
    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(headless=True, args=_browser_args())
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await safe_edit(status_msg, "🌐 Loading page...")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=120000)
            except Exception:
                pass
            try:
                for sel in [
                    'button:has-text("I AM 18")',
                    'button:has-text("ENTER")',
                    'button:has-text("Yes")',
                    ".age-gate button",
                    "button.y",
                ]:
                    try:
                        el = page.locator(sel).first
                        if await el.is_visible(timeout=1000):
                            await el.click()
                            await asyncio.sleep(2)
                            break
                    except Exception:
                        continue
            except Exception:
                pass
            await safe_edit(status_msg, "📜 Scrolling to load all images...")
            await page.evaluate("""
                async () => {
                    const delay = ms => new Promise(r => setTimeout(r, ms));
                    const totalHeight = document.body.scrollHeight;
                    const step = Math.floor(window.innerHeight * 0.8);
                    let current = 0;
                    while (current < totalHeight) {
                        window.scrollTo(0, current);
                        await delay(300);
                        current += step;
                    }
                    window.scrollTo(0, totalHeight);
                    await delay(500);
                }
            """)
            await asyncio.sleep(4)
            await safe_edit(status_msg, "📄 Rendering PDF...")
            filepath = os.path.join(OUTPUT_FOLDER, f"pdf_{int(time.time())}.pdf")
            await page.pdf(
                path=filepath,
                format="A4",
                print_background=True,
                margin={"top": "10mm", "bottom": "10mm", "left": "8mm", "right": "8mm"},
            )
            return filepath, None, os.path.getsize(filepath)
        except Exception as e:
            err = str(e)
            if "connection closed" in err.lower() or "browser" in err.lower():
                return None, "PDF Error: Browser crashed. Please try again.", 0
            return None, f"PDF Error: {err[:80]}", 0
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass


# ====================== CAPTURE MHTML ======================
async def capture_mhtml(
    url: str, status_msg: Message
) -> Tuple[Optional[str], Optional[str], int]:
    async with async_playwright() as p:
        browser = None
        try:
            await safe_edit(status_msg, "🌐 Capturing full webpage as MHTML...")
            browser = await p.chromium.launch(headless=True, args=_browser_args())
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(3)
            cdp = await context.new_cdp_session(page)
            result = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            mhtml_data = result.get("data", "")
            if not mhtml_data:
                return None, "Failed to capture MHTML", 0
            filepath = os.path.join(OUTPUT_FOLDER, f"page_{int(time.time())}.mhtml")
            async with aiofiles.open(filepath, "w", encoding="utf-8") as f:
                await f.write(mhtml_data)
            return filepath, None, os.path.getsize(filepath)
        except Exception as e:
            return None, f"MHTML Error: {str(e)[:80]}", 0
        finally:
            if browser:
                await browser.close()


# ====================== VIDEO COMPRESSION ======================
async def _run_ffmpeg(args: list) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    return proc.returncode, stderr.decode(errors="replace")


async def get_video_info(input_path: str) -> Tuple[Optional[float], int, int]:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        input_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None, 0, 0
    try:
        info = json.loads(stdout.decode())
        dur = float(info.get("format", {}).get("duration", 0))
        w, h = 0, 0
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                w = int(s.get("width", 0))
                h = int(s.get("height", 0))
                if not dur:
                    dur = float(s.get("duration", 0))
                break
        return dur or None, w, h
    except Exception:
        return None, 0, 0


async def compress_video(
    input_path: str, target_size_bytes: int, status_msg: Message
) -> Tuple[Optional[str], str]:
    target_mb = target_size_bytes / 1024 / 1024
    output_path = os.path.join(
        OUTPUT_FOLDER, f"compressed_{int(target_mb)}mb_{int(time.time())}.mp4"
    )
    passlog = os.path.join(OUTPUT_FOLDER, f"passlog_{int(time.time())}")

    await safe_edit(status_msg, "🔍 Analyzing video...")

    try:
        duration, width, height = await get_video_info(input_path)
        if not duration or duration <= 0:
            return None, "Could not read video duration."

        audio_bitrate_bps = 64_000 if target_size_bytes <= 20 * 1024 * 1024 else 128_000
        total_bitrate_bps = int((target_size_bytes * 8) / duration * 0.95)
        video_bitrate_bps = max(total_bitrate_bps - audio_bitrate_bps, 10_000)
        audio_bitrate_k = audio_bitrate_bps // 1000

        # FIX: scale + format=yuv420p + noautorotate
        SCALE_VF = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
        COMMON_INPUT = ["-noautorotate", "-i", input_path]

        await safe_edit(
            status_msg,
            f"⚙️ Compressing to ≈ {human_readable_size(target_size_bytes)}\n"
            f"📊 Duration: {int(duration)}s  |  Video: {video_bitrate_bps // 1000}kbps\n"
            f"🔄 Pass 1/2...",
        )

        pass1_args = [
            "ffmpeg",
            "-y",
            *COMMON_INPUT,
            "-vf",
            SCALE_VF,
            "-c:v",
            "libx264",
            "-b:v",
            str(video_bitrate_bps),
            "-pass",
            "1",
            "-passlogfile",
            passlog,
            "-an",
            "-f",
            "null",
            "/dev/null",
        ]
        rc, err = await _run_ffmpeg(pass1_args)

        if rc != 0:
            logger.warning(f"Two-pass pass1 failed → single-pass CRF. err: {err[:200]}")
            await safe_edit(status_msg, "⚙️ Single-pass encoding (CRF mode)...")
            sp_args = [
                "ffmpeg",
                "-y",
                *COMMON_INPUT,
                "-vf",
                SCALE_VF,
                "-c:v",
                "libx264",
                "-crf",
                "28",
                "-maxrate",
                str(video_bitrate_bps),
                "-bufsize",
                str(video_bitrate_bps * 2),
                "-preset",
                "fast",
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_bitrate_k}k",
                "-movflags",
                "+faststart",
                output_path,
            ]
            rc2, err2 = await _run_ffmpeg(sp_args)
            if rc2 != 0:
                return None, f"FFmpeg error: {err2[-300:]}"
        else:
            await safe_edit(
                status_msg,
                f"⚙️ Compressing to ≈ {human_readable_size(target_size_bytes)}\n"
                f"📊 Duration: {int(duration)}s  |  Video: {video_bitrate_bps // 1000}kbps\n"
                f"🔄 Pass 2/2...",
            )
            pass2_args = [
                "ffmpeg",
                "-y",
                *COMMON_INPUT,
                "-vf",
                SCALE_VF,
                "-c:v",
                "libx264",
                "-b:v",
                str(video_bitrate_bps),
                "-pass",
                "2",
                "-passlogfile",
                passlog,
                "-preset",
                "fast",
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_bitrate_k}k",
                "-movflags",
                "+faststart",
                output_path,
            ]
            rc, err = await _run_ffmpeg(pass2_args)
            if rc != 0:
                return None, f"FFmpeg pass2 error: {err[-300:]}"

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return None, "Output file is empty or missing."

        return (
            output_path,
            f"✅ Compressed: {human_readable_size(os.path.getsize(output_path))}",
        )

    except FileNotFoundError:
        return None, "ffmpeg/ffprobe not found. Please install ffmpeg on the server."
    except Exception as e:
        logger.error(f"Compression error: {e}", exc_info=True)
        return None, f"Unexpected error: {str(e)[:150]}"
    finally:
        for ext in [".log", "-0.log", "-0.log.mbtree"]:
            try:
                pp = passlog + ext
                if os.path.exists(pp):
                    os.remove(pp)
            except Exception:
                pass


# ====================== DIRPY FLOW ======================
processing_messages = set()


async def process_dirpy_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    logger.info(f"[DIRPY] START | chat={event.chat_id} | url={url[:120]}")
    status_msg = await event.reply("🔄 Starting extraction...", parse_mode="markdown")
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        # ===== اگه لینک یوتیوب بود → y2mate =====
        if _is_youtube_source(url):
            await _start_y2mate_flow(event, status_msg, url)
            return

        (
            found_urls,
            session_headers,
            intercept_err,
            video_title,
        ) = await extract_video_url_smart(url, status_msg)
        if not found_urls:
            logger.warning(
                f"[DIRPY] No URLs found | chat={event.chat_id} | err={intercept_err}"
            )
            await safe_edit(status_msg, f"❌ Could not capture video:\n{intercept_err}")
            return
        logger.info(f"[DIRPY] Found {len(found_urls)} URLs | chat={event.chat_id}")
        if len(found_urls) == 1:
            await do_download_and_send(
                event,
                status_msg,
                found_urls[0],
                url,
                extra_headers=session_headers,
                title=video_title,
            )
            return
        await safe_edit(
            status_msg, f"🔍 Found {len(found_urls)} links, checking sizes..."
        )
        sized_urls = []
        for u in found_urls:
            sz = await get_file_size(u)
            sized_urls.append((u, sz))
        pick_id = f"pick_{event.chat_id}_{int(time.time())}"
        video_cache[pick_id] = {
            "urls": sized_urls,
            "source_url": url,
            "chat_id": event.chat_id,
            "session_headers": session_headers,
            "title": video_title,
        }
        buttons = [
            [Button.inline(_url_label(u, sz, i), f"pickurl_{pick_id}_{i}")]
            for i, (u, sz) in enumerate(sized_urls)
        ]
        await safe_edit(status_msg, "📋 **Select video to download:**")
        await event.client.send_message(
            event.chat_id,
            f"🎬 Found **{len(sized_urls)}** video links.\nChoose one to download:",
            buttons=buttons,
            parse_mode="markdown",
        )
        await status_msg.delete()
    except Exception as e:
        logger.error(f"Dirpy process error: {e}", exc_info=True)
        err_str = str(e)
        if "connection closed" in err_str.lower() or "browser" in err_str.lower():
            await safe_edit(status_msg, "❌ Browser crashed. Please try again.")
        else:
            await safe_edit(status_msg, f"❌ Error: {err_str[:120]}")
    finally:
        processing_messages.discard(msg_id)


# ====================== CALLBACK HANDLERS ======================
async def compress_callback(event):
    video_id = event.data.decode().replace("compress_", "")
    if video_id not in video_cache:
        return await event.answer("Video not found or expired.", alert=True)
    await event.answer("Send desired size (e.g: 15mb or 800kb)", alert=False)
    # FIX: chat_id رو ذخیره میکنیم (نه sender_id) — در private chat یکیه ولی در گروه فرق دارن
    user_state[event.chat_id] = {
        "action": "wait_for_compression_size",
        "video_id": video_id,
    }


async def check_callback(event):
    video_id = event.data.decode().replace("check_", "")
    if video_id not in video_cache:
        return await event.answer("Video already deleted.", alert=True)
    data = video_cache[video_id]
    try:
        if os.path.exists(data["filepath"]):
            os.remove(data["filepath"])
        await event.answer("✅ Video deleted from server.", alert=False)
        await event.edit(buttons=None)
    except Exception:
        await event.answer("Error deleting file.", alert=True)
    video_cache.pop(video_id, None)


async def pickurl_callback(event):
    parts = event.data.decode().rsplit("_", 1)
    idx = int(parts[1])
    pick_id = parts[0].replace("pickurl_", "")
    if pick_id not in video_cache:
        return await event.answer(
            "Session expired. Please resend /dirpy command.", alert=True
        )
    data = video_cache[pick_id]
    if idx >= len(data["urls"]):
        return await event.answer("Invalid selection.", alert=True)
    chosen_url, _ = data["urls"][idx]
    source_url = data["source_url"]
    session_headers = data.get("session_headers", {})
    saved_title = data.get("title", "")
    await event.answer(f"Starting download #{idx + 1}...", alert=False)
    try:
        await event.delete()
    except Exception:
        pass
    status_msg = await event.client.send_message(
        event.chat_id, "📥 Starting download..."
    )
    del video_cache[pick_id]
    await do_download_and_send(
        event,
        status_msg,
        chosen_url,
        source_url,
        extra_headers=session_headers,
        title=saved_title,
    )


# ====================== ADMIN HANDLERS ======================
async def admin_input_handler(event):
    if event.sender_id != ADMIN_ID:
        return
    if event.sender_id not in admin_pending_add:
        return
    action = admin_pending_add.pop(event.sender_id)
    raw = event.raw_text.strip()
    if not raw.isdigit():
        await event.reply(
            "❌ Invalid ID! Please send a numeric ID only.", parse_mode="markdown"
        )
        raise events.StopPropagation
    uid = int(raw)
    if action == "add":
        if uid in AUTHORIZED_USERS:
            await event.reply(
                f"⚠️ User `{uid}` is already authorized.", parse_mode="markdown"
            )
        else:
            AUTHORIZED_USERS.add(uid)
            await event.reply(
                f"✅ User `{uid}` added!\nTotal: **{len(AUTHORIZED_USERS)}**",
                parse_mode="markdown",
            )
    elif action == "remove":
        if uid == ADMIN_ID:
            await event.reply("❌ You cannot remove yourself!", parse_mode="markdown")
        elif uid not in AUTHORIZED_USERS:
            await event.reply(f"⚠️ User `{uid}` not found.", parse_mode="markdown")
        else:
            AUTHORIZED_USERS.discard(uid)
            await event.reply(
                f"✅ User `{uid}` removed!\nTotal: **{len(AUTHORIZED_USERS)}**",
                parse_mode="markdown",
            )
    raise events.StopPropagation


# ====================== SIZE INPUT HANDLER ======================
async def size_input_handler(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    # FIX: chat_id (نه sender_id) — اصلاح اصلی برای "Invalid size format" bug
    state = user_state.get(event.chat_id)
    if not state or state.get("action") != "wait_for_compression_size":
        return

    video_id = state["video_id"]
    if video_id not in video_cache:
        user_state.pop(event.chat_id, None)
        raise events.StopPropagation

    target_bytes = parse_size_input(event.raw_text)
    if not target_bytes:
        await event.reply(
            "❌ Invalid size format!\nExamples: `15mb`, `800kb`, `1.5gb`",
            parse_mode="markdown",
        )
        raise events.StopPropagation

    data = video_cache[video_id]
    if target_bytes >= data["original_size"]:
        await event.reply(
            "❌ Target size must be smaller than original size.", parse_mode="markdown"
        )
        raise events.StopPropagation

    # state رو قبل از شروع پاک کن — جلوگیری از double-trigger
    user_state.pop(event.chat_id, None)

    status_msg = await event.reply(
        f"⚙️ Starting compression → {human_readable_size(target_bytes)}..."
    )
    compressed_path, result = await compress_video(
        data["filepath"], target_bytes, status_msg
    )

    if compressed_path and os.path.exists(compressed_path):
        await safe_edit(status_msg, "📤 Uploading compressed video...")
        try:
            comp_size = os.path.getsize(compressed_path)
            gh_line = ""
            if GITHUB_ENABLED:
                await safe_edit(status_msg, "☁️ Uploading to GitHub...")
                gh_url = await maybe_upload_github(
                    event.client, event.chat_id, compressed_path, comp_size
                )
                if gh_url:
                    gh_line = f"\n☁️ [GitHub DL]({gh_url})"
            await send_file_with_progress(
                client=event.client,
                chat_id=event.chat_id,
                filepath=compressed_path,
                caption=(
                    f"✅ **Compressed Video**\n"
                    f"🎯 Requested: {human_readable_size(target_bytes)}\n"
                    f"📦 Final Size: {human_readable_size(comp_size)}"
                    f"{gh_line}"
                ),
                status_msg=status_msg,
            )
        except Exception as e:
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
        try:
            os.remove(compressed_path)
            os.remove(data["filepath"])
        except Exception:
            pass
    else:
        await safe_edit(status_msg, f"❌ Compression failed: {result}")

    video_cache.pop(video_id, None)
    raise events.StopPropagation


# ====================== PDF & HTML COMMANDS ======================


async def _fetch_hd_url(
    post_url: str, thumb_url: str, session: aiohttp.ClientSession
) -> str:
    """برای یه post URL لینک عکس اصلی رو میگیره (برای سایت‌هایی مثل rule34)."""
    try:
        async with session.get(post_url, timeout=ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return thumb_url
            html = await resp.text()
            # rule34: id="image" src="..."
            m = re.search(
                r"id=[\x22\x27]image[\x22\x27][^>]*src=[\x22\x27]([^\x22\x27]+)[\x22\x27]",
                html,
            )
            if not m:
                m = re.search(
                    r"src=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*id=[\x22\x27]image[\x22\x27]",
                    html,
                )
            if m:
                src = m.group(1)
                if src.startswith("//"):
                    src = "https:" + src
                return src
    except Exception:
        pass
    return thumb_url


async def process_pdfimg_request(event, url: str):
    """عکس‌های صفحه رو دانلود، grid preview میسازه، دو دکمه Send All / Send All HD داره."""
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    logger.info(f"[PDFIMG] START | chat={event.chat_id} | url={url[:120]}")
    status = await event.reply("🌐 Loading page...", parse_mode="markdown")
    tmp_dir = f"/app/output_files/pdfimg_{event.chat_id}_{event.id}"

    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        os.makedirs(tmp_dir, exist_ok=True)

        # ---- مرحله 1: استخراج URL عکس‌ها + لینک post اصلی با playwright ----
        img_data = []  # list of {"thumb": url, "post": url_or_None, "orig": url_or_None}

        JS_EXTRACT = """() => {
            const results = [];
            const seen = new Set();
            document.querySelectorAll('img').forEach(img => {
                const src = img.src || img.getAttribute('data-src') ||
                            img.getAttribute('data-original') ||
                            img.getAttribute('data-lazy') || '';
                if (!src || !src.startsWith('http') || seen.has(src)) return;
                seen.add(src);
                const a = img.closest('a');
                const postUrl = a ? a.href : null;
                const origSrc = img.getAttribute('data-original-url') ||
                                img.getAttribute('data-full') || null;
                results.push({thumb: src, post: postUrl, orig: origSrc});
            });
            return results;
        }"""

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=_browser_args())
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                },
                java_script_enabled=True,
                bypass_csp=True,
            )
            page = await context.new_page()
            page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

            await safe_edit(status, "🌐 Opening page...")
            load_ok = False
            for wait_mode in ("domcontentloaded", "commit"):
                try:
                    await page.goto(url, wait_until=wait_mode, timeout=45000)
                    load_ok = True
                    break
                except Exception as _e:
                    logger.warning(f"[PDFIMG] goto({wait_mode}) failed: {_e}")

            if not load_ok:
                await browser.close()
                await safe_edit(
                    status, "❌ Could not load the page (timeout or blocked)."
                )
                return

            await page.wait_for_timeout(3000)

            # Cloudflare challenge detection
            for _cf_attempt in range(6):
                title = await page.title()
                if (
                    "just a moment" in title.lower()
                    or "checking your browser" in title.lower()
                    or "please wait" in title.lower()
                ):
                    await safe_edit(
                        status, f"⏳ Bypassing protection... ({_cf_attempt + 1}/6)"
                    )
                    await page.wait_for_timeout(5000)
                else:
                    break

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            await page.wait_for_timeout(1500)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

            img_data = await page.evaluate(JS_EXTRACT)
            await browser.close()

        if not img_data:
            logger.warning(f"[PDFIMG] No images found on page | chat={event.chat_id}")
            await safe_edit(status, "No images found on this page.")
            return

        logger.info(
            f"[PDFIMG] Found {len(img_data)} images on page | chat={event.chat_id}"
        )
        await safe_edit(status, f"Found {len(img_data)} images. Downloading...")

        # ---- مرحله 2: دانلود thumbnail ها (JPG/PNG) + ذخیره GIF به همان فرمت ----
        import io as _io
        from PIL import Image as PILImage

        saved = []  # list of {"path": str, "is_gif": bool, "thumb_url": str, "post_url": str|None}
        dl_headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": url,
        }
        connector = aiohttp.TCPConnector(ssl=False, limit=8)
        async with aiohttp.ClientSession(
            connector=connector, headers=dl_headers, timeout=ClientTimeout(total=20)
        ) as http:
            for i, item in enumerate(img_data[:300]):
                thumb_url = item["thumb"]
                post_url = item.get("post")
                orig_url = item.get("orig")
                try:
                    async with http.get(thumb_url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.read()
                        ct = resp.content_type or ""

                        is_gif = ct == "image/gif" or thumb_url.lower().endswith(".gif")

                        if is_gif:
                            # GIF رو همون‌طور ذخیره کن
                            gif_path = f"{tmp_dir}/img_{len(saved):04d}.gif"
                            async with aiofiles.open(gif_path, "wb") as f:
                                await f.write(data)
                            saved.append(
                                {
                                    "path": gif_path,
                                    "is_gif": True,
                                    "thumb_url": thumb_url,
                                    "post_url": post_url,
                                    "orig_url": orig_url,
                                }
                            )
                        else:
                            img = PILImage.open(_io.BytesIO(data)).convert("RGB")
                            if img.width < 80 or img.height < 80:
                                continue
                            img_path = f"{tmp_dir}/img_{len(saved):04d}.jpg"
                            img.save(img_path, "JPEG", quality=92)
                            img.close()
                            saved.append(
                                {
                                    "path": img_path,
                                    "is_gif": False,
                                    "thumb_url": thumb_url,
                                    "post_url": post_url,
                                    "orig_url": orig_url,
                                }
                            )

                        if len(saved) % 10 == 0:
                            await safe_edit(
                                status, f"Downloaded {len(saved)} images..."
                            )
                except Exception:
                    continue

        if not saved:
            await safe_edit(status, "Could not download any valid images.")
            return

        # ---- مرحله 3: ذخیره session و نمایش دکمه‌ها ----
        session_key = f"pdfimg_{event.chat_id}_{event.id}"
        pdfimg_sessions[session_key] = {
            "items": saved,
            "tmp_dir": tmp_dir,
            "chat_id": event.chat_id,
            "source_url": url,
        }

        n = len(saved)
        n_gif = sum(1 for s in saved if s["is_gif"])
        info = f"🖼 **{n} media ready**"
        if n_gif:
            info += f" ({n_gif} GIF)"
        info += "\nChoose how to send:"

        await status.delete()
        await event.client.send_message(
            event.chat_id,
            info,
            parse_mode="markdown",
            buttons=[
                [
                    Button.inline(f"📨 Send All ({n})", f"pdfimg_send|{session_key}"),
                    Button.inline(f"🔷 Send All HD ({n})", f"pdfimg_hd|{session_key}"),
                ],
                [Button.inline("🗑 Delete from server", f"pdfimg_del|{session_key}")],
            ],
        )

    except Exception as e:
        logger.error(f"pdfimg error: {e}", exc_info=True)
        err = str(e)
        if "connection closed" in err.lower() or "browser" in err.lower():
            await safe_edit(status, "❌ Browser crashed. Please try again.")
        else:
            await safe_edit(status, f"❌ Error: {err[:200]}")
    finally:
        processing_messages.discard(msg_id)


async def process_pdf_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    logger.info(f"[PDF] START | chat={event.chat_id} | url={url[:120]}")
    status = await event.reply("📄 Converting to PDF...", parse_mode="markdown")
    filepath = None
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        filepath, error, size = await html_to_pdf(url, status)
        if error:
            await safe_edit(status, f"❌ {error}")
            return
        gh_line = ""
        if GITHUB_ENABLED:
            await safe_edit(status, "☁️ Uploading to GitHub...")
            gh_url = await maybe_upload_github(
                event.client, event.chat_id, filepath, size
            )
            if gh_url:
                gh_line = f"\n☁️ [GitHub DL]({gh_url})"
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=f"📑 PDF • {human_readable_size(size)}{gh_line}",
            force_document=True,
        )
        await status.delete()
    except Exception as e:
        await safe_edit(status, f"❌ Unexpected error: {str(e)[:120]}")
    finally:
        processing_messages.discard(msg_id)
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


async def process_html_request(event, url: str):
    msg_id = f"{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    logger.info(f"[HTML] START | chat={event.chat_id} | url={url[:120]}")
    status = await event.reply("🌐 Capturing full webpage...", parse_mode="markdown")
    filepath = None
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        filepath, error, size = await capture_mhtml(url, status)
        if error:
            await safe_edit(status, f"❌ {error}")
            return
        gh_line = ""
        if GITHUB_ENABLED:
            await safe_edit(status, "☁️ Uploading to GitHub...")
            gh_url = await maybe_upload_github(
                event.client, event.chat_id, filepath, size
            )
            if gh_url:
                gh_line = f"\n☁️ [GitHub DL]({gh_url})"
        await event.client.send_file(
            event.chat_id,
            filepath,
            caption=f"📦 Complete Webpage Snapshot (MHTML){gh_line}",
        )
        await status.delete()
    except Exception as e:
        await safe_edit(status, f"❌ Unexpected error: {str(e)[:120]}")
    finally:
        processing_messages.discard(msg_id)
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass


# ====================== TELEGRAM COMMANDS ======================
async def admin_cmd(event):
    logger.info(f"[CMD] /admin from user={event.sender_id}")
    if event.sender_id != ADMIN_ID:
        return await event.reply("⛔ Unauthorized")
    users_list = "\n".join([f"• `{uid}`" for uid in sorted(AUTHORIZED_USERS)])
    await event.reply(
        f"👑 **Admin Panel**\n\n**Authorized Users ({len(AUTHORIZED_USERS)}):**\n{users_list}\n\nChoose an action:",
        parse_mode="markdown",
        buttons=[
            [Button.inline("➕ Add User", "admin_add")],
            [Button.inline("➖ Remove User", "admin_remove")],
            [Button.inline("🔄 Refresh List", "admin_refresh")],
        ],
    )


async def admin_add_callback(event):
    if event.sender_id != ADMIN_ID:
        return await event.answer("Unauthorized", alert=True)
    admin_pending_add[event.sender_id] = "add"
    await event.answer("", alert=False)
    await event.client.send_message(
        event.chat_id,
        "📩 Send me the **numeric user ID** to add:",
        parse_mode="markdown",
        buttons=[[Button.inline("❌ Cancel", "admin_cancel")]],
    )


async def admin_remove_callback(event):
    if event.sender_id != ADMIN_ID:
        return await event.answer("Unauthorized", alert=True)
    admin_pending_add[event.sender_id] = "remove"
    await event.answer("", alert=False)
    await event.client.send_message(
        event.chat_id,
        "📩 Send me the **numeric user ID** to remove:",
        parse_mode="markdown",
        buttons=[[Button.inline("❌ Cancel", "admin_cancel")]],
    )


async def admin_refresh_callback(event):
    if event.sender_id != ADMIN_ID:
        return await event.answer("Unauthorized", alert=True)
    users_list = "\n".join([f"• `{uid}`" for uid in sorted(AUTHORIZED_USERS)])
    await event.answer("✅ Refreshed", alert=False)
    try:
        await event.edit(
            f"👑 **Admin Panel**\n\n**Authorized Users ({len(AUTHORIZED_USERS)}):**\n{users_list}\n\nChoose an action:",
            parse_mode="markdown",
            buttons=[
                [Button.inline("➕ Add User", "admin_add")],
                [Button.inline("➖ Remove User", "admin_remove")],
                [Button.inline("🔄 Refresh List", "admin_refresh")],
            ],
        )
    except Exception:
        pass


async def admin_cancel_callback(event):
    if event.sender_id != ADMIN_ID:
        return await event.answer("Unauthorized", alert=True)
    admin_pending_add.pop(event.sender_id, None)
    await event.answer("Cancelled", alert=False)
    try:
        await event.delete()
    except Exception:
        pass


async def startgithub_cmd(event):
    global GITHUB_ENABLED
    logger.info(f"[CMD] /startgithub from user={event.sender_id}")
    if event.sender_id != ADMIN_ID:
        return await event.reply("⛔ Unauthorized")
    if not github_configured():
        return await event.reply("❌ GitHub not configured (token or repo missing)")
    GITHUB_ENABLED = True
    await event.reply(
        "✅ **GitHub upload ENABLED**\n\n"
        f"📁 Repo: `{GITHUB_REPO}`\n"
        f"🌿 Branch: `{GITHUB_BRANCH}`\n"
        f"📦 Max size: `{GITHUB_MAX_MB}MB`\n\n"
        "From now on, all files sent by the bot will also be uploaded to GitHub with a direct download link.",
        parse_mode="markdown",
    )


async def stopgithub_cmd(event):
    global GITHUB_ENABLED
    logger.info(f"[CMD] /stopgithub from user={event.sender_id}")
    if event.sender_id != ADMIN_ID:
        return await event.reply("⛔ Unauthorized")
    GITHUB_ENABLED = False
    await event.reply(
        "🔴 **GitHub upload DISABLED**\nFiles will no longer be uploaded to GitHub.",
        parse_mode="markdown",
    )


async def github_cmd(event):
    logger.info(f"[CMD] /github from user={event.sender_id}")
    if event.sender_id != ADMIN_ID:
        return await event.reply("⛔ Unauthorized")
    status_icon = "✅ Active" if GITHUB_ENABLED else "⏸ Paused"
    if github_configured():
        await event.reply(
            f"☁️ **GitHub Status: {status_icon}**\n\n"
            f"📁 Repo: `{GITHUB_REPO}`\n"
            f"🌿 Branch: `{GITHUB_BRANCH}`\n"
            f"📂 Base dir: `{GITHUB_BASE_DIR}`\n"
            f"📦 Max file size: `{GITHUB_MAX_MB}MB`\n\n"
            f"• `/startgithub` — enable auto-upload\n"
            f"• `/stopgithub` — disable auto-upload",
            parse_mode="markdown",
        )
    else:
        await event.reply(
            "☁️ **GitHub Status: ❌ Not configured**\n\n"
            "Set these environment variables:\n"
            "`GITHUB_TOKEN` — Personal Access Token\n"
            "`GITHUB_REPO` — e.g. `username/myrepo`\n"
            "`GITHUB_BRANCH` — default: `main`\n"
            "`GITHUB_BASE_DIR` — default: `files`",
            parse_mode="markdown",
        )


async def start_cmd(event):
    logger.info(f"[CMD] /start from user={event.sender_id}")
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    await event.reply(
        "🚀 **Ultimate Bot v6**\n\n"
        "• `/dirpy <url>` → Download video\n"
        "• `/yt <url>` → Download YouTube via Y2Mate\n"
        "• `/snapwc <url>` → Download via SnapWC\n"
        "• `/savep <url>` → Download via SaveTheVideo\n"
        "• `/pdf <url>` → Webpage to PDF\n"
        "• `/html <url>` → Save as MHTML\n"
        "• `/pdfimg <url>` → Download all images\n"
        "• `/github` → GitHub upload status\n"
        "• `/startgithub` → Enable GitHub upload\n"
        "• `/stopgithub` → Disable GitHub upload\n\n"
        "**YouTube links → auto yt-dlp (server-side)**\n"
        "**During download:** ⏸ Pause  •  ❌ Cancel\n"
        "**After download:** 🗜 Compress  •  ✅ Delete",
        parse_mode="markdown",
    )


async def dirpy_command(event):
    logger.info(
        f"[CMD] /dirpy from user={event.sender_id} | text={event.raw_text[:100]}"
    )
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/dirpy <url>`", parse_mode="markdown")
    await process_dirpy_request(event, parts[1].strip())


async def savep_command(event):
    logger.info(
        f"[CMD] /savep from user={event.sender_id} | text={event.raw_text[:100]}"
    )
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/savep <url>`", parse_mode="markdown")
    await process_savep_request(
        event=event,
        url=parts[1].strip(),
        safe_edit_fn=safe_edit,
        send_file_fn=send_file_with_progress,
        download_dir="/tmp",
    )


async def pdf_command(event):
    logger.info(f"[CMD] /pdf from user={event.sender_id} | text={event.raw_text[:100]}")
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/pdf <url>`", parse_mode="markdown")
    await process_pdf_request(event, parts[1].strip())


async def pdfimg_del_callback(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.answer("⛔ Unauthorized")
    session_key = event.data.decode().split("|", 1)[1]
    session = pdfimg_sessions.pop(session_key, None)
    if session:
        import shutil

        try:
            shutil.rmtree(session["tmp_dir"], ignore_errors=True)
        except Exception:
            pass
    await event.edit(buttons=None)
    await event.answer("🗑 Deleted from server.")


async def _do_send_pdfimg(event, session_key: str, hd: bool):
    """ارسال عکس‌ها — normal: thumbnail، HD: لینک اصلی از صفحه post"""
    session = pdfimg_sessions.get(session_key)
    if not session:
        return await event.answer("❌ Session expired. Run /pdfimg again.", alert=True)

    await event.answer("📨 Sending..." if not hd else "🔷 Fetching HD...", alert=False)
    items = [it for it in session["items"] if os.path.exists(it["path"])]
    chat_id = session["chat_id"]
    source_url = session.get("source_url", "")
    total = len(items)

    if total == 0:
        return await event.client.send_message(chat_id, "❌ No images found on server.")

    label = "HD" if hd else "normal"
    status = await event.client.send_message(
        chat_id, f"📨 Sending {total} files ({label})..."
    )
    sent = 0

    dl_headers = {"User-Agent": "Mozilla/5.0", "Referer": source_url}
    connector = aiohttp.TCPConnector(ssl=False, limit=4)
    import io as _io
    from PIL import Image as PILImage

    async with aiohttp.ClientSession(
        connector=connector, headers=dl_headers, timeout=ClientTimeout(total=30)
    ) as http:
        for item in items:
            try:
                send_path = item["path"]

                if hd:
                    # پیدا کردن لینک اصلی
                    hd_url = item.get("orig_url") or item["thumb_url"]

                    # اگه post_url داره، برو صفحه پست و عکس اصلی رو بگیر
                    post_url = item.get("post_url")
                    if post_url and post_url.startswith("http"):
                        fetched = await _fetch_hd_url(post_url, hd_url, http)
                        if fetched != hd_url:
                            hd_url = fetched

                    # دانلود HD
                    async with http.get(hd_url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            ct = resp.content_type or ""
                            is_gif = ct == "image/gif" or hd_url.lower().endswith(
                                ".gif"
                            )
                            ext = ".gif" if is_gif else ".jpg"
                            hd_path = (
                                item["path"]
                                .replace(".jpg", "_hd" + ext)
                                .replace(".gif", "_hd" + ext)
                            )
                            if not is_gif:
                                # convert به JPEG
                                img = PILImage.open(_io.BytesIO(data)).convert("RGB")
                                img.save(hd_path, "JPEG", quality=97)
                            else:
                                async with aiofiles.open(hd_path, "wb") as f:
                                    await f.write(data)
                            send_path = hd_path

                await event.client.send_file(
                    chat_id,
                    send_path,
                    force_document=False,
                )
                sent += 1

                # آپلود به گیتهاب اگه فعاله
                if GITHUB_ENABLED:
                    try:
                        img_size = os.path.getsize(send_path)
                        gh_url = await maybe_upload_github(
                            event.client, chat_id, send_path, img_size
                        )
                        if gh_url:
                            await event.client.send_message(
                                chat_id,
                                f"☁️ [GitHub DL]({gh_url})",
                                parse_mode="markdown",
                            )
                    except Exception:
                        pass

                if sent % 5 == 0 or sent == total:
                    try:
                        await status.edit(f"📨 Sending... {sent}/{total}")
                    except Exception:
                        pass

                # پاک کردن HD temp
                if hd and send_path != item["path"] and os.path.exists(send_path):
                    try:
                        os.remove(send_path)
                    except Exception:
                        pass

            except Exception as e:
                logger.warning(f"pdfimg send error: {e}")
                try:
                    await status.edit(f"⚠️ Error on {sent + 1}: {str(e)[:60]}")
                except Exception:
                    pass

    # cleanup
    import shutil

    pdfimg_sessions.pop(session_key, None)
    try:
        shutil.rmtree(session["tmp_dir"], ignore_errors=True)
    except Exception:
        pass
    try:
        await event.edit(buttons=None)
    except Exception:
        pass
    try:
        await status.edit(f"✅ Sent {sent}/{total} files!")
    except Exception:
        pass


async def pdfimg_send_callback(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.answer("⛔ Unauthorized")
    session_key = event.data.decode().split("|", 1)[1]
    await _do_send_pdfimg(event, session_key, hd=False)


async def pdfimg_hd_callback(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.answer("⛔ Unauthorized")
    session_key = event.data.decode().split("|", 1)[1]
    await _do_send_pdfimg(event, session_key, hd=True)


async def pdfimg_command(event):
    logger.info(
        f"[CMD] /pdfimg from user={event.sender_id} | text={event.raw_text[:100]}"
    )
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/pdfimg <url>`", parse_mode="markdown")
    await process_pdfimg_request(event, parts[1].strip())


async def html_command(event):
    logger.info(
        f"[CMD] /html from user={event.sender_id} | text={event.raw_text[:100]}"
    )
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/html <url>`", parse_mode="markdown")
    await process_html_request(event, parts[1].strip())


async def generic_url_handler(event):
    if event.sender_id not in AUTHORIZED_USERS or event.raw_text.startswith("/"):
        return
    if (
        event.chat_id in user_state
        and user_state[event.chat_id].get("action") == "wait_for_compression_size"
    ):
        return
    urls = re.findall(r'https?://[^\s<>"\']+', event.raw_text)
    if not urls:
        return
    target_url = urls[0]
    logger.info(
        f"[URL] Direct URL received | chat={event.chat_id} | url={target_url[:120]}"
    )

    # ===== اگه لینک یوتیوب بود → برو به y2mate =====
    if _is_youtube_source(target_url):
        status_msg = await event.reply("🎬 YouTube detected — starting Y2Mate...")
        await _start_y2mate_flow(event, status_msg, target_url)
        return

    dl_id = f"dl_{event.chat_id}_{event.id}_{int(time.time())}"
    active_downloads[dl_id] = {"paused": False, "cancelled": False}
    status_msg = await event.reply("⏬ Downloading...")

    filepath, error, size = await download_with_controls(
        target_url, status_msg, dl_id, referer=target_url
    )

    # FIX: 403 → auto-dirpy (فقط برای لینک صفحه ویدیو، نه فایل مستقیم)
    if error == "HTTP_403":
        from urllib.parse import urlparse
        import os as _os

        _ext = _os.path.splitext(urlparse(target_url).path)[1]
        if _ext:
            await safe_edit(
                status_msg, f"❌ Server returned 403 (blocked) for this file."
            )
            return
        await safe_edit(status_msg, "🔄 403 — extracting via Dirpy...")
        await process_dirpy_request(event, target_url)
        try:
            await status_msg.delete()
        except Exception:
            pass
        return

    if error or not filepath:
        if error != "Cancelled by user" and is_stream_url(target_url):
            stream_fp, stream_err = await try_stream_download(
                target_url, status_msg, referer=target_url
            )
            if stream_fp:
                filepath, error = stream_fp, None
                size = os.path.getsize(stream_fp)
            else:
                await safe_edit(status_msg, f"❌ {stream_err}")
                return
        elif error != "Cancelled by user":
            await safe_edit(status_msg, f"❌ {error or 'Failed'}")
            return
        else:
            return

    import os as _os_audio2

    # تشخیص نوع فایل با ffprobe
    vid_duration, vw, vh = await get_video_info(filepath)

    # اگه فقط صدا داره → بصورت ویس بفرست
    _audio_exts2 = (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac", ".opus")
    _is_audio2 = (vid_duration and vid_duration > 0 and vw == 0 and vh == 0) or (
        (not vid_duration or vid_duration <= 0)
        and _os_audio2.path.splitext(filepath)[1].lower() in _audio_exts2
    )
    if _is_audio2:
        await safe_edit(status_msg, "🎵 Uploading audio...")
        try:
            _vd2 = vid_duration or 0
            basename = os.path.basename(filepath)
            await event.client.send_file(
                event.chat_id,
                filepath,
                caption=basename,
                attributes=[DocumentAttributeAudio(duration=int(_vd2), title=basename)],
                supports_streaming=True,
            )
        except Exception as e:
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
        try:
            os.remove(filepath)
        except Exception:
            pass
        return

    # اگه ویدیو نیست → آپلود به عنوان فایل
    if vid_duration is None or vid_duration <= 0:
        basename = os.path.basename(filepath)
        await safe_edit(status_msg, "📤 Uploading file...")
        try:
            await event.client.send_file(
                event.chat_id,
                filepath,
                caption=f"📎 **{basename}**\n📦 Size: {human_readable_size(size)}",
                force_document=True,
            )
        except Exception as e:
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
        try:
            os.remove(filepath)
        except Exception:
            pass
        return

    await safe_edit(status_msg, "📤 Uploading...")
    try:
        dur_str = ""
        if vid_duration and vid_duration > 0:
            mins, secs = divmod(int(vid_duration), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                dur_str = f" | ⏱ {hours}:{mins:02d}:{secs:02d}"
            else:
                dur_str = f" | ⏱ {mins}:{secs:02d}"
        gh_line = ""
        if GITHUB_ENABLED:
            await safe_edit(status_msg, "☁️ Uploading to GitHub...")
            gh_url = await maybe_upload_github(
                event.client, event.chat_id, filepath, size
            )
            if gh_url:
                gh_line = f"\n☁️ [GitHub DL]({gh_url})"
            await safe_edit(status_msg, "📤 Uploading...")
        await send_file_with_progress(
            client=event.client,
            chat_id=event.chat_id,
            filepath=filepath,
            caption=f"📦 {human_readable_size(size)}{dur_str}{gh_line}",
            status_msg=status_msg,
        )
    except Exception as e:
        await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
        return
    try:
        os.remove(filepath)
    except Exception:
        pass


# ====================== VIDEO RECEIVE → GITHUB OFFER ======================


async def video_receive_handler(event):
    """وقتی کاربر ویدیو میفرسته و GITHUB_ENABLED فعاله، یه دکمه پیشنهاد آپلود به گیتهاب میده."""
    if event.sender_id not in AUTHORIZED_USERS:
        return
    if not GITHUB_ENABLED:
        return
    # فقط ویدیو — document های غیر ویدیو رو رد کن
    media = event.video or event.document
    if not media:
        return
    # بررسی mime type
    mime = getattr(media, "mime_type", "") or ""
    if not mime.startswith("video/") and not (event.video):
        return
    file_size = getattr(media, "size", 0) or 0
    if file_size == 0 or file_size > GITHUB_MAX_MB * 1024 * 1024:
        return  # بزرگتر از حد مجاز — نادیده بگیر

    pending_id = f"vgh_{event.chat_id}_{event.id}_{int(time.time())}"
    video_github_pending[pending_id] = {
        "chat_id": event.chat_id,
        "message_id": event.id,
        "file_size": file_size,
    }

    size_str = human_readable_size(file_size)
    await event.reply(
        f"☁️ **GitHub Upload**\n"
        f"📦 Size: {size_str}\n\n"
        f"Do you want to upload this video to GitHub and get a direct download link?",
        parse_mode="markdown",
        buttons=[
            [
                Button.inline("✅ Yes, upload to GitHub", f"vgh_yes_{pending_id}"),
                Button.inline("❌ No", f"vgh_no_{pending_id}"),
            ]
        ],
    )


async def vgh_yes_callback(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.answer("⛔ Unauthorized", alert=True)
    pending_id = event.data.decode().replace("vgh_yes_", "")
    data = video_github_pending.pop(pending_id, None)
    if not data:
        return await event.answer("❌ Session expired.", alert=True)

    await event.answer("⏳ Downloading and uploading...", alert=False)
    try:
        await event.edit("⏳ Downloading video from Telegram...", buttons=None)
    except Exception:
        pass

    # دانلود ویدیو از تلگرام
    tmp_path = os.path.join(OUTPUT_FOLDER, f"vgh_{int(time.time())}.mp4")
    try:
        msg = await event.client.get_messages(data["chat_id"], ids=data["message_id"])
        await event.client.download_media(msg, file=tmp_path)
    except Exception as e:
        try:
            await event.edit(f"❌ Download failed: {str(e)[:100]}", buttons=None)
        except Exception:
            pass
        return

    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        try:
            await event.edit("❌ Failed to download video from Telegram.", buttons=None)
        except Exception:
            pass
        return

    actual_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
    size_mb = actual_size / (1024 * 1024)
    from github import CONTENT_API_MAX_MB as _CMAX

    if size_mb > _CMAX:
        upload_note = (
            f"📦 {size_mb:.1f} MB — using Releases API (may take a few minutes)..."
        )
    else:
        upload_note = f"📦 {size_mb:.1f} MB — uploading..."
    try:
        await event.edit(f"☁️ **Uploading to GitHub**\n{upload_note}", buttons=None)
    except Exception:
        pass

    gh_ok, gh_msg, gh_url = await upload_to_github(tmp_path)

    try:
        os.remove(tmp_path)
    except Exception:
        pass

    if gh_ok and gh_url:
        try:
            await event.edit(
                f"✅ **Uploaded to GitHub!**\n\n"
                f"🔗 [Direct Download Link]({gh_url})\n"
                f"`{gh_url}`",
                parse_mode="markdown",
                buttons=None,
            )
        except Exception:
            pass
    else:
        try:
            await event.edit(f"❌ GitHub upload failed:\n{gh_msg[:200]}", buttons=None)
        except Exception:
            pass


async def vgh_no_callback(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.answer("⛔ Unauthorized", alert=True)
    pending_id = event.data.decode().replace("vgh_no_", "")
    video_github_pending.pop(pending_id, None)
    await event.answer("OK", alert=False)
    try:
        await event.delete()
    except Exception:
        pass


# ====================== SNAPWC HANDLERS ======================


async def snapwc_command(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/snapwc <url>`", parse_mode="markdown")

    url = parts[1].strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    status_msg = await event.reply("🔄 Starting SnapWC session...")
    logger.info(f"[SNAPWC] START | chat={event.chat_id} | url={url[:120]}")

    session = SnapWCSession()
    try:
        result = await asyncio.wait_for(session.run_full_flow(url), timeout=180)

        if not result["success"]:
            steps = result.get("steps", [])
            err = result.get("error", "Unknown")
            log = "\n".join(f"  • {s}" for s in steps)
            logger.error(f"[SNAPWC] run_full_flow failed: {err} | steps: {log}")
            await safe_edit(status_msg, f"❌ SnapWC error: {err}")
            ss = result.get("screenshot_b64", "")
            if ss:
                try:
                    await event.client.send_file(
                        event.chat_id,
                        base64.b64decode(ss),
                        caption=f"📸 SnapWC screenshot: {err[:80]}",
                    )
                except Exception:
                    pass
            await session.close_browser()
            return

        qualities = result.get("qualities", [])
        if not qualities:
            await safe_edit(status_msg, "❌ No quality options found.")
            await session.close_browser()
            return

        session_id = f"snapwc_{event.chat_id}_{event.id}_{int(time.time())}"
        snapwc_sessions[session_id] = session
        user_state[event.chat_id] = {
            "action": "snapwc_quality",
            "session_id": session_id,
            "video_url": url,
        }

        grouped = {"Video": [], "No Sound": [], "Audio": []}
        for q in qualities:
            cat = q["category"]
            if cat in grouped:
                grouped[cat].append(q)

        msg_lines = [f"🎬 **SnapWC — {len(qualities)} options found:**\n"]
        cat_icons = {"Video": "🎬", "No Sound": "🔇", "Audio": "🎵"}
        idx = 1
        buttons = []
        for cat in ["Video", "No Sound", "Audio"]:
            items = grouped.get(cat, [])
            if not items:
                continue
            msg_lines.append(f"\n{cat_icons[cat]} **{cat}**")
            for q in items:
                sz = f" ({q['size']})" if q.get("size") else ""
                msg_lines.append(f"  {idx}. {q['label']}{sz}")
                btn_emoji = cat_icons.get(q["category"], "📁")
                buttons.append(
                    [
                        Button.inline(
                            f"{btn_emoji} {q['label']}{sz}",
                            f"snapwc_q_{session_id}_{q['index']}",
                        )
                    ]
                )
                idx += 1

        buttons.append([Button.inline("❌ Cancel", f"snapwc_cancel_{session_id}")])

        await safe_edit(
            status_msg,
            "\n".join(msg_lines),
            buttons=buttons,
        )

    except Exception as e:
        logger.error(f"[SNAPWC] Command error: {e}", exc_info=True)
        await safe_edit(status_msg, f"❌ SnapWC error: {str(e)[:120]}")
        try:
            ss = await session.take_screenshot()
            if ss:
                await event.client.send_file(
                    event.chat_id,
                    base64.b64decode(ss),
                    caption=f"📸 SnapWC error screenshot",
                )
        except Exception:
            pass
        try:
            await session.close_browser()
        except Exception:
            pass


async def snapwc_select_callback(event):
    data = event.data.decode()
    prefix_removed = data.replace("snapwc_q_", "")
    session_id = prefix_removed.rsplit("_", 1)[0]
    index = int(prefix_removed.rsplit("_", 1)[1])

    if session_id not in snapwc_sessions:
        return await event.answer("❌ Session expired. Run /snapwc again.", alert=True)

    session = snapwc_sessions.pop(session_id, None)
    if not session:
        return await event.answer("❌ Session expired. Run /snapwc again.", alert=True)

    await event.answer("⏳ Processing...", alert=False)

    try:
        result = await session.continue_with_quality(index)

        if result.get("captcha"):
            captcha_b64 = result["captcha_image"]
            if "," in captcha_b64:
                raw_b64 = captcha_b64.split(",", 1)[1]
            else:
                raw_b64 = captcha_b64
            captcha_data = base64.b64decode(raw_b64)

            captcha_path = os.path.join(OUTPUT_FOLDER, f"captcha_{session_id}.png")
            async with aiofiles.open(captcha_path, "wb") as f:
                await f.write(captcha_data)

            await event.client.send_file(
                event.chat_id,
                captcha_path,
                caption="🔐 **Captcha detected!**\nPlease enter the code from the image.",
                buttons=[Button.inline("❌ Cancel", f"snapwc_cancel_{session_id}")],
            )

            try:
                os.remove(captcha_path)
            except Exception:
                pass

            snapwc_sessions[session_id] = session
            user_state[event.chat_id] = {
                "action": "snapwc_captcha",
                "session_id": session_id,
                "selected_index": index,
                "video_url": user_state.get(event.chat_id, {}).get("video_url", ""),
            }

            await safe_edit(event, "🔐 Captcha required — check the image sent above.")
            return

        if result["success"]:
            download_url = result["download_url"]
            title = result.get("title", "")

            steps = result.get("steps", [])
            logger.info(f"[SNAPWC] Quality selected OK | steps: {' → '.join(steps)}")

            status_msg = await event.client.send_message(
                event.chat_id, "✅ Got download link! Downloading..."
            )

            video_url = user_state.get(event.chat_id, {}).get("video_url", "")
            dl_ok = await do_download_and_send(
                event, status_msg, download_url, video_url, title=title
            )

            # Even if download failed, send the direct link to user
            if not dl_ok and download_url:
                try:
                    await event.client.send_message(
                        event.chat_id,
                        f"⬇️ **Direct download link (try manually):**\n`{download_url}`\n_Links may expire quickly._",
                        parse_mode="markdown",
                        link_preview=False,
                    )
                except Exception:
                    pass

            # Retry once on failure: get fresh URL from SnapWC
            if not dl_ok and video_url:
                retry_msg = await event.client.send_message(
                    event.chat_id,
                    f"🔄 **Retrying SnapWC — fresh download link...**\nPrevious error logged.",
                )
                logger.info(
                    f"[SNAPWC] Retry started | index={index} | url={video_url[:80]}"
                )
                new_session = None
                try:
                    new_session = SnapWCSession()
                    await safe_edit(retry_msg, "🔄 Step 1/3: Loading SnapWC...")
                    new_result = await new_session.run_full_flow(video_url)
                    if new_result["success"]:
                        await safe_edit(retry_msg, "🔄 Step 2/3: Selecting quality...")
                        new_dl = await new_session.continue_with_quality(index)
                        if new_dl.get("success") and not new_dl.get("captcha"):
                            fresh_url = new_dl["download_url"]
                            fresh_title = new_dl.get("title", title)
                            await safe_edit(
                                retry_msg, "🔄 Step 3/3: Retrying download..."
                            )
                            retry_ok = await do_download_and_send(
                                event,
                                retry_msg,
                                fresh_url,
                                video_url,
                                title=fresh_title,
                            )
                            if not retry_ok:
                                await safe_edit(
                                    retry_msg,
                                    "❌ Retry also failed. SnapWC may be having issues.",
                                )
                        elif new_dl.get("captcha"):
                            await safe_edit(
                                retry_msg, "🔐 Captcha on retry — run /snapwc again."
                            )
                        else:
                            err = new_dl.get("error", "Unknown")
                            steps = " → ".join(new_dl.get("steps", []))
                            logger.error(
                                f"[SNAPWC] Retry continue_with_quality failed: {err} | steps: {steps}"
                            )
                            await safe_edit(retry_msg, f"❌ Retry failed: {err}")
                    else:
                        err = new_result.get("error", "Unknown")
                        steps = " → ".join(new_result.get("steps", []))
                        logger.error(
                            f"[SNAPWC] Retry run_full_flow failed: {err} | steps: {steps}"
                        )
                        await safe_edit(retry_msg, f"❌ SnapWC retry failed: {err}")
                except Exception as retry_e:
                    logger.error(f"[SNAPWC] Retry error: {retry_e}", exc_info=True)
                    await safe_edit(retry_msg, f"❌ Retry error: {str(retry_e)[:120]}")
                finally:
                    if new_session:
                        try:
                            await new_session.close_browser()
                        except Exception:
                            pass

            user_state.pop(event.chat_id, None)
        else:
            err = result.get("error", "Unknown")
            steps = result.get("steps", [])
            log = "\n".join(f"  • {s}" for s in steps)
            logger.error(f"[SNAPWC] continue_with_quality failed: {err}\n{log}")
            await safe_edit(event, f"❌ Error: {err}")
            ss = result.get("screenshot_b64", "")
            if ss:
                try:
                    await event.client.send_file(
                        event.chat_id,
                        base64.b64decode(ss),
                        caption=f"📸 SnapWC screenshot: {err[:80]}",
                    )
                except Exception:
                    pass
            user_state.pop(event.chat_id, None)

    except Exception as e:
        logger.error(f"[SNAPWC] Select callback error: {e}", exc_info=True)
        await safe_edit(event, f"❌ Error: {str(e)[:120]}")
        snapwc_sessions.pop(session_id, None)
        user_state.pop(event.chat_id, None)


async def snapwc_captcha_handler(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return
    state = user_state.get(event.chat_id)
    if not state or state.get("action") != "snapwc_captcha":
        return

    session_id = state.get("session_id", "")
    index = state.get("selected_index", 0)
    code = event.raw_text.strip()

    if session_id not in snapwc_sessions:
        await event.reply("❌ Session expired. Please run /snapwc again.")
        user_state.pop(event.chat_id, None)
        raise events.StopPropagation

    session = snapwc_sessions[session_id]
    status_msg = await event.reply("⏳ Submitting captcha...")

    try:
        result = await session.continue_after_captcha(code, index)

        if result["success"]:
            download_url = result["download_url"]
            title = result.get("title", "")

            await safe_edit(status_msg, "✅ Captcha solved! Starting download...")
            video_url = state.get("video_url", "")
            await do_download_and_send(
                event, status_msg, download_url, video_url, title=title
            )
        else:
            await safe_edit(status_msg, f"❌ {result.get('error', 'Captcha failed')}")
    except Exception as e:
        logger.error(f"[SNAPWC] Captcha error: {e}", exc_info=True)
        await safe_edit(status_msg, f"❌ Error: {str(e)[:120]}")
    finally:
        snapwc_sessions.pop(session_id, None)
        user_state.pop(event.chat_id, None)

    raise events.StopPropagation


async def snapwc_cancel_callback(event):
    data = event.data.decode()
    session_id = data.replace("snapwc_cancel_", "")
    if session_id in snapwc_sessions:
        session = snapwc_sessions.pop(session_id)
        try:
            await session.close_browser()
        except Exception:
            pass
    user_state.pop(event.chat_id, None)
    await event.answer("❌ Cancelled", alert=False)
    try:
        await event.edit("❌ SnapWC session cancelled.", buttons=None)
    except Exception:
        pass


# ====================== Y2MATE HANDLERS ======================


async def _start_y2mate_flow(event, status_msg, video_url: str):
    logger.info(f"[Y2MATE] START | chat={event.chat_id} | url={video_url[:120]}")

    session = Y2MateSession()
    try:
        result = await asyncio.wait_for(session.run_full_flow(video_url), timeout=180)

        if not result["success"]:
            err = result.get("error", "Unknown")
            logger.error(f"[Y2MATE] run_full_flow failed: {err}")
            await safe_edit(status_msg, f"❌ Y2Mate error: {err}")
            ss = result.get("screenshot_b64", "")
            if ss:
                try:
                    await event.client.send_file(
                        event.chat_id,
                        base64.b64decode(ss),
                        caption=f"📸 Y2Mate screenshot: {err[:80]}",
                    )
                except Exception:
                    pass
            await session.close_browser()
            return

        qualities = result.get("qualities", [])
        if not qualities:
            await safe_edit(status_msg, "❌ No quality options found.")
            await session.close_browser()
            return

        session_id = f"y2mate_{event.chat_id}_{event.id}_{int(time.time())}"
        y2mate_sessions[session_id] = session
        user_state[event.chat_id] = {
            "action": "y2mate_quality",
            "session_id": session_id,
            "video_url": video_url,
        }

        msg_lines = [f"🎬 **Y2Mate — {len(qualities)} options:**\n"]
        buttons = []
        for q in qualities:
            sz = f" ({q['size']})" if q.get("size") else ""
            ext = "🎵" if q["format"] == "mp3" else "🎬"
            msg_lines.append(f"  {q['index']}. {q['label']}{sz}")
            buttons.append(
                [
                    Button.inline(
                        f"{ext} {q['label']}{sz}", f"yt_q_{session_id}_{q['index']}"
                    )
                ]
            )
        buttons.append([Button.inline("❌ Cancel", f"yt_cancel_{session_id}")])

        await safe_edit(status_msg, "\n".join(msg_lines), buttons=buttons)

    except Exception as e:
        logger.error(f"[Y2MATE] Error: {e}", exc_info=True)
        await safe_edit(status_msg, f"❌ Y2Mate error: {str(e)[:120]}")
        try:
            await session.close_browser()
        except Exception:
            pass


async def y2mate_command(event):
    if event.sender_id not in AUTHORIZED_USERS:
        return await event.reply("⛔ Unauthorized")
    parts = event.raw_text.split(maxsplit=1)
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/yt <url>`", parse_mode="markdown")
    url = parts[1].strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    status_msg = await event.reply("🔄 Starting Y2Mate session...")
    await _start_y2mate_flow(event, status_msg, url)


async def yt_select_callback(event):
    data = event.data.decode()
    prefix_removed = data.replace("yt_q_", "")
    session_id = prefix_removed.rsplit("_", 1)[0]
    index = int(prefix_removed.rsplit("_", 1)[1])

    if session_id not in y2mate_sessions:
        return await event.answer("❌ Session expired. Run /yt again.", alert=True)
    session = y2mate_sessions.pop(session_id, None)
    if not session:
        return await event.answer("❌ Session expired.", alert=True)

    await event.answer("⏳ Getting download link...", alert=False)
    try:
        result = await session.select_quality(index)
        if result["success"]:
            download_url = result["download_url"]
            logger.info(f"[Y2MATE] Got download URL: {download_url[:100]}")

            status_msg = await event.client.send_message(
                event.chat_id, "✅ Got link! Downloading..."
            )
            video_url = user_state.get(event.chat_id, {}).get("video_url", "")

            dl_ok = await do_download_and_send(
                event,
                status_msg,
                download_url,
                video_url,
                title=session.title_text,
                description="",
                skip_ytdlp=True,
                fallback_ext=session.qualities[index]["format"],
            )
            if not dl_ok and download_url:
                try:
                    await event.client.send_message(
                        event.chat_id,
                        f"⬇️ **Direct link (try manually):**\n`{download_url}`",
                        parse_mode="markdown",
                        link_preview=False,
                    )
                except Exception:
                    pass
        else:
            err = result.get("error", "Unknown")
            logger.error(f"[Y2MATE] select_quality failed: {err}")
            await safe_edit(event, f"❌ Error: {err}")
            ss = result.get("screenshot_b64", "")
            if ss:
                try:
                    await event.client.send_file(
                        event.chat_id,
                        base64.b64decode(ss),
                        caption=f"📸 Y2Mate screenshot: {err[:80]}",
                    )
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"[Y2MATE] Select callback error: {e}", exc_info=True)
        await safe_edit(event, f"❌ Error: {str(e)[:120]}")
    finally:
        user_state.pop(event.chat_id, None)
        try:
            await session.close_browser()
        except Exception:
            pass


async def yt_cancel_callback(event):
    data = event.data.decode()
    session_id = data.replace("yt_cancel_", "")
    if session_id in y2mate_sessions:
        session = y2mate_sessions.pop(session_id)
        try:
            await session.close_browser()
        except Exception:
            pass
    user_state.pop(event.chat_id, None)
    await event.answer("❌ Cancelled", alert=False)
    try:
        await event.edit("❌ Y2Mate session cancelled.", buttons=None)
    except Exception:
        pass


# ====================== MAIN ======================
async def main():
    print("\n" + "=" * 60)
    print("🚀 ULTIMATE BOT v6")
    print("   FIX 1: YouTube IP-lock → server-side yt-dlp")
    print("   FIX 2: 403 → auto-retry via Dirpy")
    print("   FIX 3: FFmpeg -noautorotate + yuv420p")
    print("   FIX 4: size_input uses chat_id (not sender_id)")
    print("   FIX 5: pause/resume split callbacks")
    print("   FIX 6: detailed logging enabled")
    print("=" * 60)
    logger.info("[BOOT] Starting bot...")

    start_keep_alive()
    client = TelegramClient(
        "ultimate_bot_session",
        API_ID,
        API_HASH,
        connection_retries=5,
    )
    for attempt in range(5):
        try:
            await client.start(bot_token=BOT_TOKEN)
            break
        except FloodWaitError as e:
            wait = e.seconds + 5
            logger.warning(
                f"[BOOT] FloodWait — waiting {wait}s before retry (attempt {attempt + 1}/5)"
            )
            await asyncio.sleep(wait)
    else:
        logger.critical("[BOOT] Could not connect after 5 FloodWait retries. Exiting.")
        return

    # ===== CallbackQuery handlers =====
    client.add_event_handler(
        dl_pause_callback, events.CallbackQuery(pattern=r"dlpause_(.+)")
    )
    client.add_event_handler(
        dl_resume_callback, events.CallbackQuery(pattern=r"dlresume_(.+)")
    )
    client.add_event_handler(
        dl_cancel_callback, events.CallbackQuery(pattern=r"dlcancel_(.+)")
    )
    client.add_event_handler(
        compress_callback, events.CallbackQuery(pattern=r"compress_(.+)")
    )
    client.add_event_handler(
        check_callback, events.CallbackQuery(pattern=r"check_(.+)")
    )
    client.add_event_handler(
        pickurl_callback, events.CallbackQuery(pattern=r"pickurl_(.+)_(\d+)$")
    )
    client.add_event_handler(
        admin_add_callback, events.CallbackQuery(pattern=r"admin_add")
    )
    client.add_event_handler(
        admin_remove_callback, events.CallbackQuery(pattern=r"admin_remove")
    )
    client.add_event_handler(
        admin_refresh_callback, events.CallbackQuery(pattern=r"admin_refresh")
    )
    client.add_event_handler(
        admin_cancel_callback, events.CallbackQuery(pattern=r"admin_cancel")
    )
    client.add_event_handler(
        pdfimg_del_callback, events.CallbackQuery(pattern=rb"pdfimg_del\|")
    )
    client.add_event_handler(
        pdfimg_send_callback, events.CallbackQuery(pattern=rb"pdfimg_send\|")
    )
    client.add_event_handler(
        pdfimg_hd_callback, events.CallbackQuery(pattern=rb"pdfimg_hd\|")
    )
    client.add_event_handler(
        vgh_yes_callback, events.CallbackQuery(pattern=r"vgh_yes_(.+)")
    )
    client.add_event_handler(
        vgh_no_callback, events.CallbackQuery(pattern=r"vgh_no_(.+)")
    )
    client.add_event_handler(
        snapwc_select_callback, events.CallbackQuery(pattern=r"snapwc_q_(.+)")
    )
    client.add_event_handler(
        snapwc_cancel_callback, events.CallbackQuery(pattern=r"snapwc_cancel_(.+)")
    )
    client.add_event_handler(
        yt_select_callback, events.CallbackQuery(pattern=r"yt_q_(.+)")
    )
    client.add_event_handler(
        yt_cancel_callback, events.CallbackQuery(pattern=r"yt_cancel_(.+)")
    )

    # ===== Command handlers =====
    client.add_event_handler(
        start_cmd, events.NewMessage(pattern=r"^/start(\s|$)", incoming=True)
    )
    client.add_event_handler(
        startgithub_cmd,
        events.NewMessage(pattern=r"^/startgithub(\s|$)", incoming=True),
    )
    client.add_event_handler(
        stopgithub_cmd, events.NewMessage(pattern=r"^/stopgithub(\s|$)", incoming=True)
    )
    client.add_event_handler(
        github_cmd, events.NewMessage(pattern=r"^/github(\s|$)", incoming=True)
    )
    client.add_event_handler(
        admin_cmd, events.NewMessage(pattern=r"^/admin(\s|$)", incoming=True)
    )
    client.add_event_handler(
        dirpy_command, events.NewMessage(pattern=r"^/dirpy(\s|$)", incoming=True)
    )
    client.add_event_handler(
        snapwc_command, events.NewMessage(pattern=r"^/snapwc(\s|$)", incoming=True)
    )
    client.add_event_handler(
        y2mate_command, events.NewMessage(pattern=r"^/yt(\s|$)", incoming=True)
    )
    client.add_event_handler(
        savep_command, events.NewMessage(pattern=r"^/savep(\s|$)", incoming=True)
    )
    client.add_event_handler(
        pdf_command, events.NewMessage(pattern=r"^/pdf(\s|$)", incoming=True)
    )
    client.add_event_handler(
        pdfimg_command, events.NewMessage(pattern=r"^/pdfimg(\s|$)", incoming=True)
    )
    client.add_event_handler(
        html_command, events.NewMessage(pattern=r"^/html(\s|$)", incoming=True)
    )

    # ===== Message handlers (order matters - specific before generic) =====
    client.add_event_handler(admin_input_handler, events.NewMessage(incoming=True))
    client.add_event_handler(size_input_handler, events.NewMessage(incoming=True))
    client.add_event_handler(
        video_receive_handler,
        events.NewMessage(incoming=True, func=lambda e: bool(e.video or e.document)),
    )
    client.add_event_handler(snapwc_captcha_handler, events.NewMessage(incoming=True))
    client.add_event_handler(generic_url_handler, events.NewMessage(incoming=True))

    me = await client.get_me()
    logger.info(f"[BOOT] Bot connected as @{me.username} (id={me.id})")
    logger.info(f"[BOOT] Authorized users: {AUTHORIZED_USERS}")
    logger.info(
        f"[BOOT] GitHub enabled: {GITHUB_ENABLED} | repo: {GITHUB_REPO if github_configured() else 'not configured'}"
    )
    print(f"✅ Bot is online → @{me.username}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        import uvloop

        uvloop.run(main())
    except ImportError:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped.")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
