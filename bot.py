#!/usr/bin/env python3
# Telegram Ultimate Bot - v5
# Fixes: 403 auto-dirpy + FFmpeg scale/rotation fix + size_input chat_id fix + pause/resume split

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
    DocumentAttributeVideo,
    DocumentAttributeAudio,
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
from savep_handler import process_savep_request, trigger_savep_cancel
from snapwc_handler import SnapWCSession
from y2mate import Y2MateSession
from youtube_extractor import extract_youtube_info

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
y2mate_sessions: Dict[str, dict] = {}  # Y2Mate session cache

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
async def download_with_controls(
    url: str,
    status_msg: Message,
    dl_id: str,
    referer: Optional[str] = None,
    extra_headers: Optional[dict] = None,
) -> Tuple[Optional[str], Optional[str], int]:
    MAX_RETRIES = 3
    CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunks

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer
        try:
            headers["Origin"] = "/".join(referer.split("/")[:3])
        except Exception:
            pass
    if extra_headers:
        headers.update(extra_headers)

    timeout = ClientTimeout(total=None, connect=30, sock_read=120)
    filepath = ""
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
                    if response.status == 403:
                        return None, "HTTP_403", 0
                    if response.status not in (200, 206):
                        return None, f"HTTP {response.status}", 0

                    ct = (response.headers.get("Content-Type", "") or "").lower()
                    if "text/html" in ct:
                        return (
                            None,
                            "Got HTML page instead of file (redirected to ad)",
                            0,
                        )

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

                        # Detect original filename
                        orig_name = ""
                        cd = response.headers.get("Content-Disposition", "")
                        if "filename=" in cd:
                            fm = re.search(r'filename="?([^";]+)', cd)
                            if fm:
                                orig_name = fm.group(1).strip()
                        if not orig_name:
                            url_path = url.split("?")[0].rstrip("/")
                            orig_name = os.path.basename(url_path)
                        if not orig_name:
                            orig_name = f"file_{int(time.time())}"
                        orig_name = re.sub(r"[^\w\.\-_\(\) ]", "_", orig_name)
                        if len(orig_name) > 80:
                            orig_name = orig_name[:80]

                        # Detect extension
                        ext = os.path.splitext(orig_name)[1].lower()
                        if not ext:
                            ct = (
                                response.headers.get("Content-Type", "") or ""
                            ).lower()
                            ct_map = {
                                "video/mp4": ".mp4",
                                "video/x-matroska": ".mkv",
                                "video/webm": ".webm",
                                "video/avi": ".avi",
                                "video/quicktime": ".mov",
                                "video/x-msvideo": ".avi",
                                "audio/mpeg": ".mp3",
                                "audio/mp4": ".m4a",
                                "audio/ogg": ".ogg",
                                "audio/wav": ".wav",
                                "audio/x-wav": ".wav",
                                "audio/flac": ".flac",
                                "image/jpeg": ".jpg",
                                "image/png": ".png",
                                "image/gif": ".gif",
                                "image/webp": ".webp",
                                "application/pdf": ".pdf",
                                "application/zip": ".zip",
                                "application/x-rar-compressed": ".rar",
                                "application/x-7z-compressed": ".7z",
                                "application/x-tar": ".tar",
                                "application/gzip": ".gz",
                            }
                            for mtype, mext in ct_map.items():
                                if mtype in ct:
                                    ext = mext
                                    break
                        if not ext:
                            ext = ".mp4"
                        orig_name = os.path.splitext(orig_name)[0] + ext

                        filepath = os.path.join(OUTPUT_FOLDER, orig_name)
                        # Avoid overwrite: add suffix if exists
                        counter = 1
                        while os.path.exists(filepath):
                            base = os.path.splitext(orig_name)[0]
                            filepath = os.path.join(
                                OUTPUT_FOLDER, f"{base}_{counter}{ext}"
                            )
                            counter += 1

                    write_mode = "ab" if downloaded > 0 else "wb"
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
            # Reject tiny files — likely error/placeholder, not real video
            if downloaded < 1024:
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                return None, f"File too small ({downloaded} B) — not a real video", 0
            # Check first bytes for HTML content (ad/error page disguised as video)
            try:
                with open(filepath, "rb") as _f:
                    head = _f.read(512)
                if head.lstrip(b"\xef\xbb\xbf")[:1] == b"<":
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                    return None, "Downloaded HTML page instead of video", 0
            except Exception:
                pass
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
    thumb_filepath: str = None,
):
    file_size = os.path.getsize(filepath)
    start_time = time.time()
    last_update = [0.0]
    last_bytes = [0]
    last_time = [start_time]
    ext = os.path.splitext(filepath)[1].lower()

    duration, width, height = await get_video_info(filepath)
    is_video = duration is not None and duration > 0 and width > 0 and height > 0
    is_audio = ext in (".mp3", ".m4a", ".ogg", ".wav", ".flac", ".aac", ".wma", ".opus")

    # ---- Preprocessing ----
    orig_filepath = filepath
    tmp_files = []
    thumb_path = None

    try:
        # ویدیو: moov atom رو ببر اول فایل (Fast Start) برای استریمینگ
        if is_video:
            fast_path = filepath + "_faststart.mp4"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i",
                filepath,
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-y",
                fast_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if os.path.exists(fast_path) and os.path.getsize(fast_path) > 0:
                filepath = fast_path
                tmp_files.append(fast_path)

        # صدا: استخراج کاور از تگ‌های ID3
        audio_title = ""
        audio_performer = ""
        if is_audio and not thumb_filepath:
            cover_path = filepath + "_cover.jpg"
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i",
                filepath,
                "-an",
                "-vcodec",
                "copy",
                "-y",
                cover_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
                thumb_filepath = cover_path
                tmp_files.append(cover_path)

        # متادیتای صدا (عنوان و هنرمند)
        if is_audio:
            probe = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                orig_filepath,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await probe.communicate()
            try:
                tags = json.loads(out.decode()).get("format", {}).get("tags", {})
                audio_title = tags.get("title", "")
                audio_performer = tags.get("artist", "") or tags.get("TPE1", "")
            except Exception:
                pass

        thumb_path = thumb_filepath or (
            await get_video_thumbnail(filepath) if is_video else None
        )

        async def progress_cb(current: int, total: int):
            now = time.time()
            if now - last_update[0] < 3.0 and current != total:
                return
            last_update[0] = now
            dt = now - last_time[0]
            speed = (current - last_bytes[0]) / dt if dt > 0 else 0
            last_bytes[0] = current
            last_time[0] = now
            text = build_progress_text(
                "📤 Uploading", current, total, speed, start_time
            )
            try:
                asyncio.ensure_future(status_msg.edit(text, parse_mode="markdown"))
            except Exception:
                pass

        sent = None
        with open(filepath, "rb") as f:
            uploaded = await fast_upload_file(
                client, f, progress_callback=progress_cb, connection_count=15
            )

        if is_video:
            duration_int = int(duration) if duration else 0
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
            if thumb_path and os.path.exists(thumb_path):
                with open(thumb_path, "rb") as tf:
                    thumb_input = await fast_upload_file(client, tf)
            media = InputMediaUploadedDocument(
                file=uploaded,
                mime_type=mime_type,
                attributes=attributes,
                thumb=thumb_input,
                force_file=False,
            )
        elif is_audio:
            audio_dur = int(duration) if duration and duration > 0 else 0
            attributes, mime_type = utils.get_attributes(
                filepath,
                attributes=[
                    DocumentAttributeAudio(
                        duration=audio_dur,
                        voice=False,
                        title=audio_title or None,
                        performer=audio_performer or None,
                    )
                ],
            )
            thumb_input = None
            if thumb_path and os.path.exists(thumb_path):
                with open(thumb_path, "rb") as tf:
                    thumb_input = await fast_upload_file(client, tf)
            media = InputMediaUploadedDocument(
                file=uploaded,
                mime_type=mime_type,
                attributes=attributes,
                thumb=thumb_input,
                force_file=False,
            )
        else:
            attributes, mime_type = utils.get_attributes(filepath)
            media = InputMediaUploadedDocument(
                file=uploaded,
                mime_type=mime_type,
                attributes=attributes,
                force_file=True,
            )

        sent = await client.send_file(
            chat_id,
            media,
            caption=caption,
            buttons=buttons,
            parse_mode="markdown",
        )
    finally:
        if thumb_path and os.path.exists(thumb_path) and thumb_path != thumb_filepath:
            try:
                os.remove(thumb_path)
            except Exception:
                pass
        for fp in tmp_files:
            try:
                if os.path.exists(fp):
                    os.remove(fp)
            except Exception:
                pass

    try:
        await status_msg.delete()
    except Exception:
        pass

    return sent


# ====================== DOWNLOAD AND SEND ======================
async def do_download_and_send(
    event,
    status_msg,
    direct_url: str,
    source_url: str,
    extra_headers: Optional[dict] = None,
    title: str = "",
) -> bool:
    dl_id = f"dl_{event.chat_id}_{event.id}_{int(time.time())}"
    active_downloads[dl_id] = {"paused": False, "cancelled": False}

    filepath, dl_error, final_size = await download_with_controls(
        direct_url, status_msg, dl_id, referer=source_url, extra_headers=extra_headers
    )

    # FIX: 403 → auto-retry via dirpy
    if dl_error == "HTTP_403":
        await safe_edit(status_msg, "🔄 403 received — extracting via Dirpy...")
        (
            found_urls,
            session_headers,
            intercept_err,
            page_title,
        ) = await extract_video_url_smart(source_url, status_msg)
        if not found_urls:
            await safe_edit(
                status_msg, f"❌ Could not extract via Dirpy either:\n{intercept_err}"
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
        )

    if dl_error or not filepath:
        if dl_error != "Cancelled by user":
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
        return False

    await safe_edit(status_msg, "📤 Uploading...")
    try:
        ext = os.path.splitext(filepath)[1].lower()
        vid_duration, vw, vh = await get_video_info(filepath)
        is_video = vid_duration is not None and vid_duration > 0 and vw > 0 and vh > 0

        if is_video:
            mins, secs = divmod(int(vid_duration), 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                dur_str = f"\n⏱ Duration: {hours}:{mins:02d}:{secs:02d}"
            else:
                dur_str = f"\n⏱ Duration: {mins}:{secs:02d}"
            caption_start = f"🎬 {title}" if title else "🎬 **Video Downloaded**"
        else:
            dur_str = ""
            fname = os.path.basename(filepath)
            caption_start = f"📄 **{fname}**" if not title else f"📄 **{title}**"

        gh_line = ""
        if GITHUB_ENABLED:
            await safe_edit(status_msg, "☁️ Uploading to GitHub...")
            gh_url = await maybe_upload_github(
                event.client, event.chat_id, filepath, final_size
            )
            if gh_url:
                gh_line = f"\n☁️ [GitHub DL]({gh_url})"

        await send_file_with_progress(
            client=event.client,
            chat_id=event.chat_id,
            filepath=filepath,
            caption=(
                f"{caption_start}\n"
                f"📦 Size: {human_readable_size(final_size)}"
                f"{dur_str}\n"
                f"🔗 [Source]({source_url})\n"
                f"⬇️ [DW Link]({direct_url})"
                f"{gh_line}"
            ),
            status_msg=status_msg,
            supports_streaming=True,
        )
        try:
            os.remove(filepath)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
        return False
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
        # - format=yuv420p: مطمئن میشه pixel format با libx264 سازگاره
        # - noautorotate: جلوگیری از تداخل rotation metadata با scale filter
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
        "🚀 **Ultimate Bot v5**\n\n"
        "• `/dirpy <url>` → Download video\n"
        "• `/snapwc <url>` → Download via SnapWC\n"
        "• `/savep <url>` → Download via SaveTheVideo\n"
        "• `/pdf <url>` → Webpage to PDF\n"
        "• `/html <url>` → Save as MHTML\n"
        "• `/pdfimg <url>` → Download all images\n"
        "• `/github` → GitHub upload status\n"
        "• `/startgithub` → Enable GitHub upload\n"
        "• `/stopgithub` → Disable GitHub upload\n\n"
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
        download_dir=OUTPUT_FOLDER,
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
    msg_id = f"gen_{event.chat_id}_{event.id}"
    if msg_id in processing_messages:
        return
    processing_messages.add(msg_id)
    urls = re.findall(r'https?://[^\s<>"\']+', event.raw_text)
    if not urls:
        processing_messages.discard(msg_id)
        return
    target_url = urls[0]

    if (
        YOUTUBE_RE.match(target_url)
        or "youtube.com" in target_url
        or "youtu.be" in target_url
    ):
        logger.info(f"[URL] YouTube detected | url={target_url[:120]}")
        status_msg = await event.reply("⏬ Processing...")
        try:
            await process_y2mate_request(event, target_url, status_msg)
        finally:
            processing_messages.discard(msg_id)
        return

    if (
        "pornhub.com" in target_url
        or "xnxx.com" in target_url
        or "xvideos.com" in target_url
        or "xhamster.com" in target_url
    ):
        logger.info(
            f"[URL] Adult site detected, routing via SaveTheVideo | url={target_url[:120]}"
        )
        await process_savep_request(
            event=event,
            url=target_url,
            safe_edit_fn=safe_edit,
            send_file_fn=send_file_with_progress,
            download_dir=OUTPUT_FOLDER,
        )
        processing_messages.discard(msg_id)
        return

    logger.info(
        f"[URL] Direct URL received | chat={event.chat_id} | url={target_url[:120]}"
    )
    dl_id = f"dl_{event.chat_id}_{event.id}_{int(time.time())}"
    active_downloads[dl_id] = {"paused": False, "cancelled": False}
    status_msg = await event.reply("⏬ Downloading...")
    try:
        filepath, error, size = await download_with_controls(
            target_url, status_msg, dl_id, referer=target_url
        )

        if error == "HTTP_403":
            await safe_edit(status_msg, "🔄 403 — extracting via Dirpy...")
            await process_dirpy_request(event, target_url)
            try:
                await status_msg.delete()
            except Exception:
                pass
            return

        if error or not filepath:
            if error != "Cancelled by user":
                await safe_edit(status_msg, f"❌ {error or 'Failed'}")
            return
        await safe_edit(status_msg, "📤 Uploading...")
        try:
            vid_duration, vw, vh = await get_video_info(filepath)
            is_video = (
                vid_duration is not None and vid_duration > 0 and vw > 0 and vh > 0
            )
            if is_video:
                mins, secs = divmod(int(vid_duration), 60)
                hours, mins = divmod(mins, 60)
                if hours > 0:
                    dur_str = f" | ⏱ {hours}:{mins:02d}:{secs:02d}"
                else:
                    dur_str = f" | ⏱ {mins}:{secs:02d}"
            else:
                dur_str = ""
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
    finally:
        processing_messages.discard(msg_id)


# ====================== Y2MATE INTEGRATION ======================

YOUTUBE_RE = re.compile(r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/")


async def process_y2mate_request(event, url: str, status_msg):
    logger.info(f"[Y2MATE] START | chat={event.chat_id} | url={url[:120]}")
    await safe_edit(status_msg, "🔄 Processing via Y2Mate...")
    session = Y2MateSession()
    try:
        result = await asyncio.wait_for(session.run_full_flow(url), timeout=120)
        if not result["success"]:
            await safe_edit(
                status_msg, f"❌ Y2Mate error: {result.get('error', 'Unknown')}"
            )
            ss = result.get("screenshot_b64", "")
            if ss:
                try:
                    await event.client.send_file(
                        event.chat_id, base64.b64decode(ss), caption="📸 Y2Mate error"
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

        yt_title = session.title_text or ""
        pick_id = f"{event.chat_id}_{int(time.time())}"
        y2mate_sessions[pick_id] = {
            "session": session,
            "qualities": qualities,
            "source_url": url,
            "title": yt_title,
            "chat_id": event.chat_id,
        }

        buttons = []
        row = []
        for i, q in enumerate(qualities):
            label = f"{q['label']} ({q.get('size', '?')})"
            btn = Button.inline(label, f"y2mq_{pick_id}_{i}")
            row.append(btn)
            if len(row) >= 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([Button.inline("❌ Cancel", f"y2mc_{pick_id}")])

        title_line = f"\n🎬 **{yt_title}**" if yt_title else ""
        await safe_edit(
            status_msg,
            f"📋 **Choose quality:**{title_line}",
            buttons=buttons,
        )
    except asyncio.TimeoutError:
        await safe_edit(status_msg, "❌ Y2Mate timed out (120s).")
        await session.close_browser()
    except Exception as e:
        logger.error(f"[Y2MATE] Error: {e}", exc_info=True)
        await safe_edit(status_msg, f"❌ Y2Mate error: {str(e)[:120]}")
        try:
            await session.close_browser()
        except Exception:
            pass
            await session.close_browser()
            return

        qualities = result.get("qualities", [])
        if not qualities:
            await safe_edit(status_msg, "❌ No quality options found.")
            await session.close_browser()
            return

        # Only keep video (mp4) qualities
        video_qs = [
            (i, q)
            for i, q in enumerate(qualities)
            if q.get("format", "mp4") == "mp4" and "p" in q.get("label", "").lower()
        ]
        audio_qs = [
            (i, q)
            for i, q in enumerate(qualities)
            if q.get("format") == "mp3" or "kbps" in q.get("label", "").lower()
        ]

        is_audio = False
        if video_qs:
            sel_idx, selected = video_qs[-1]
        elif audio_qs:
            sel_idx, selected = audio_qs[-1]
            is_audio = True
        else:
            sel_idx, selected = len(qualities) - 1, qualities[-1]

        await safe_edit(
            status_msg,
            f"📥 Downloading {selected['label']} ({selected.get('size', '?')})...",
        )
        dl_result = await session.select_quality(sel_idx)
        if not dl_result["success"]:
            await safe_edit(
                status_msg,
                f"❌ Y2Mate download failed: {dl_result.get('error', 'Unknown')}",
            )
            await session.close_browser()
            return

        dl_url = dl_result["download_url"]
        await session.close_browser()

        await safe_edit(status_msg, "📥 Downloading file...")
        yt_title = session.title_text or ""

        extra_ext = ".mp3" if is_audio else ".mp4"
        dl_id = f"dl_{event.chat_id}_{event.id}_{int(time.time())}"
        active_downloads[dl_id] = {"paused": False, "cancelled": False}
        filepath, dl_error, final_size = await download_with_controls(
            dl_url,
            status_msg,
            dl_id,
            referer="https://v21.www-y2mate.com/",
            extra_headers={"Referer": "https://v21.www-y2mate.com/"},
        )

        if dl_error or not filepath:
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
            return

        await safe_edit(status_msg, "📤 Uploading...")
        try:
            # Ensure correct extension for audio
            if is_audio:
                base = os.path.splitext(filepath)[0]
                new_path = base + ".mp3"
                if filepath != new_path:
                    try:
                        os.rename(filepath, new_path)
                        filepath = new_path
                    except Exception:
                        pass

            fname = os.path.basename(filepath)
            yt_clean = yt_title
            caption_start = (
                f"🎬 {yt_clean}"
                if yt_clean
                else ("🎵 Audio" if is_audio else f"📄 {fname}")
            )
            gh_line = ""
            if GITHUB_ENABLED:
                gh_url = await maybe_upload_github(
                    event.client, event.chat_id, filepath, final_size
                )
                if gh_url:
                    gh_line = f"\n☁️ [GitHub DL]({gh_url})"

            # دانلود تامبنیل یوتیوب
            thumb_fp = None
            if not is_audio and "youtube" in url.lower():
                try:
                    import re as _re

                    ym = _re.search(
                        r"(?:v=|youtu\.be/|/v/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})",
                        url,
                    )
                    if ym:
                        vid = ym.group(1)
                        turl = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
                        async with aiohttp.ClientSession() as sess:
                            async with sess.get(
                                turl, timeout=aiohttp.ClientTimeout(total=10)
                            ) as resp:
                                if resp.status == 200:
                                    tfp = filepath + "_ytthumb.jpg"
                                    async with aiofiles.open(tfp, "wb") as f:
                                        async for chunk in resp.content.iter_chunked(
                                            65536
                                        ):
                                            await f.write(chunk)
                                    if os.path.getsize(tfp) > 0:
                                        thumb_fp = tfp
                except Exception:
                    pass

            await send_file_with_progress(
                client=event.client,
                chat_id=event.chat_id,
                filepath=filepath,
                caption=f"{caption_start}\n📦 {human_readable_size(final_size)}\n🔗 [Source]({url}){gh_line}",
                status_msg=status_msg,
                thumb_filepath=thumb_fp,
            )
            if thumb_fp and os.path.exists(thumb_fp):
                try:
                    os.remove(thumb_fp)
                except Exception:
                    pass
        except Exception as e:
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
            return
        try:
            os.remove(filepath)
        except Exception:
            pass
    except asyncio.TimeoutError:
        await safe_edit(status_msg, "❌ Y2Mate timed out (120s).")
        await session.close_browser()
    except Exception as e:
        logger.error(f"[Y2MATE] Error: {e}", exc_info=True)
        await safe_edit(status_msg, f"❌ Y2Mate error: {str(e)[:120]}")
        try:
            await session.close_browser()
        except Exception:
            pass
    except asyncio.TimeoutError:
        await safe_edit(status_msg, "❌ Y2Mate timed out (120s).")
        await session.close_browser()
    except Exception as e:
        logger.error(f"[Y2MATE] Error: {e}", exc_info=True)
        await safe_edit(status_msg, f"❌ Y2Mate error: {str(e)[:120]}")
        try:
            await session.close_browser()
        except Exception:
            pass


# ====================== VIDEO RECEIVE -> GITHUB OFFER ======================


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

    await _run_snapwc_flow(event, url, status_msg)


async def _run_snapwc_flow(event, url, status_msg):

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
            download_headers = result.get("download_headers", {})
            title = result.get("title", "")
            download_data = result.get("download_data", {})

            steps = result.get("steps", [])
            logger.info(f"[SNAPWC] Quality selected OK | steps: {' → '.join(steps)}")

            # If browser already downloaded the file, send directly
            if download_data.get("browser_download") and download_data.get("filepath"):
                filepath = download_data["filepath"]
                file_size = download_data.get("file_size", 0)
                status_msg = await event.client.send_message(
                    event.chat_id, "✅ File downloaded via browser! Uploading..."
                )
                caption_start = f"🎬 {title}" if title else "📄 **SnapWC Download**"
                await send_file_with_progress(
                    client=event.client,
                    chat_id=event.chat_id,
                    filepath=filepath,
                    caption=(
                        f"{caption_start}\n📦 Size: {human_readable_size(file_size)}"
                    ),
                    status_msg=status_msg,
                )
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                user_state.pop(event.chat_id, None)
                return

            status_msg = await event.client.send_message(
                event.chat_id, "✅ Got download link! Downloading..."
            )

            video_url = user_state.get(event.chat_id, {}).get("video_url", "")
            dl_ok = await do_download_and_send(
                event,
                status_msg,
                download_url,
                video_url,
                title=title,
                extra_headers=download_headers if download_headers else None,
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
                            fresh_headers = new_dl.get("download_headers", {})
                            fresh_title = new_dl.get("title", title)
                            fresh_download_data = new_dl.get("download_data", {})

                            # If browser already downloaded the file, send directly
                            if fresh_download_data.get(
                                "browser_download"
                            ) and fresh_download_data.get("filepath"):
                                filepath = fresh_download_data["filepath"]
                                file_size = fresh_download_data.get("file_size", 0)
                                await safe_edit(
                                    retry_msg,
                                    "✅ File downloaded via browser! Uploading...",
                                )
                                caption_start = (
                                    f"🎬 {fresh_title}"
                                    if fresh_title
                                    else "📄 **SnapWC Download**"
                                )
                                await send_file_with_progress(
                                    client=event.client,
                                    chat_id=event.chat_id,
                                    filepath=filepath,
                                    caption=(
                                        f"{caption_start}\n"
                                        f"📦 Size: {human_readable_size(file_size)}"
                                    ),
                                    status_msg=retry_msg,
                                )
                                try:
                                    os.remove(filepath)
                                except Exception:
                                    pass
                            else:
                                await safe_edit(
                                    retry_msg, "🔄 Step 3/3: Retrying download..."
                                )
                                retry_ok = await do_download_and_send(
                                    event,
                                    retry_msg,
                                    fresh_url,
                                    video_url,
                                    extra_headers=fresh_headers
                                    if fresh_headers
                                    else None,
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
            download_headers = result.get("download_headers", {})
            title = result.get("title", "")
            download_data = result.get("download_data", {})

            # If browser already downloaded the file, send directly
            if download_data.get("browser_download") and download_data.get("filepath"):
                filepath = download_data["filepath"]
                file_size = download_data.get("file_size", 0)
                await safe_edit(
                    status_msg, "✅ File downloaded via browser! Uploading..."
                )
                caption_start = f"🎬 {title}" if title else "📄 **SnapWC Download**"
                await send_file_with_progress(
                    client=event.client,
                    chat_id=event.chat_id,
                    filepath=filepath,
                    caption=(
                        f"{caption_start}\n📦 Size: {human_readable_size(file_size)}"
                    ),
                    status_msg=status_msg,
                )
                try:
                    os.remove(filepath)
                except Exception:
                    pass
                return

            await safe_edit(status_msg, "✅ Captcha solved! Starting download...")
            video_url = state.get("video_url", "")
            await do_download_and_send(
                event,
                status_msg,
                download_url,
                video_url,
                extra_headers=download_headers if download_headers else None,
                title=title,
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


# ====================== Y2MATE CALLBACK HANDLERS ======================


async def y2mate_quality_callback(event):
    data = event.data.decode()
    rest = data[5:]
    idx_pos = rest.rfind("_")
    if idx_pos == -1:
        return await event.answer("Invalid callback.", alert=True)
    pick_id = rest[:idx_pos]
    try:
        idx = int(rest[idx_pos + 1 :])
    except ValueError:
        return await event.answer("Invalid quality index.", alert=True)

    if pick_id not in y2mate_sessions:
        return await event.answer("Session expired. Send link again.", alert=True)

    entry = y2mate_sessions.pop(pick_id)
    session = entry["session"]
    qualities = entry["qualities"]
    source_url = entry["source_url"]
    yt_title = entry["title"]

    try:
        await event.answer("⏬ Downloading...", alert=False)
        await event.edit("📥 Processing your selection...", buttons=None)

        q = qualities[idx]
        dl_result = await session.select_quality(idx)
        if not dl_result["success"]:
            await event.edit(f"❌ Failed: {dl_result.get('error', 'Unknown')}")
            await session.close_browser()
            return

        dl_url = dl_result["download_url"]
        await session.close_browser()

        status_msg = await event.get_message()
        await safe_edit(status_msg, "📥 Downloading file...")
        is_audio = q.get("format") == "mp3" or "kbps" in q.get("label", "").lower()
        dl_id = f"dl_{event.chat_id}_{event.id}_{int(time.time())}"
        active_downloads[dl_id] = {"paused": False, "cancelled": False}
        filepath, dl_error, final_size = await download_with_controls(
            dl_url,
            status_msg,
            dl_id,
            referer="https://v21.www-y2mate.com/",
            extra_headers={"Referer": "https://v21.www-y2mate.com/"},
        )

        if dl_error or not filepath:
            await safe_edit(status_msg, f"❌ Download failed: {dl_error}")
            return

        await safe_edit(status_msg, "📤 Uploading...")
        try:
            if is_audio:
                base = os.path.splitext(filepath)[0]
                new_path = base + ".mp3"
                if filepath != new_path:
                    try:
                        os.rename(filepath, new_path)
                        filepath = new_path
                    except Exception:
                        pass

            clean_title = (
                yt_title if yt_title and "free download" not in yt_title.lower() else ""
            )
            caption_start = (
                f"🎬 {clean_title}"
                if clean_title
                else ("🎵 Audio" if is_audio else f"📄 {os.path.basename(filepath)}")
            )
            gh_line = ""
            if GITHUB_ENABLED:
                gh_url = await maybe_upload_github(
                    event.client, event.chat_id, filepath, final_size
                )
                if gh_url:
                    gh_line = f"\n☁️ [GitHub DL]({gh_url})"

            # دانلود تامبنیل یوتیوب
            thumb_fp = None
            if not is_audio and "youtube" in source_url.lower():
                try:
                    import re as _re

                    ym = _re.search(
                        r"(?:v=|youtu\.be/|/v/|/embed/|/shorts/)([a-zA-Z0-9_-]{11})",
                        source_url,
                    )
                    if ym:
                        vid = ym.group(1)
                        turl = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
                        async with aiohttp.ClientSession() as sess:
                            async with sess.get(
                                turl, timeout=aiohttp.ClientTimeout(total=10)
                            ) as resp:
                                if resp.status == 200:
                                    tfp = filepath + "_ytthumb.jpg"
                                    async with aiofiles.open(tfp, "wb") as f:
                                        async for chunk in resp.content.iter_chunked(
                                            65536
                                        ):
                                            await f.write(chunk)
                                    if os.path.getsize(tfp) > 0:
                                        thumb_fp = tfp
                except Exception:
                    pass

            sent_msg = await send_file_with_progress(
                client=event.client,
                chat_id=event.chat_id,
                filepath=filepath,
                caption=f"{caption_start}\n📦 {human_readable_size(final_size)}\n🔗 [Source]({source_url}){gh_line}",
                status_msg=status_msg,
                thumb_filepath=thumb_fp,
            )
            # پاک کردن تامبنیل موقت
            if thumb_fp and os.path.exists(thumb_fp):
                try:
                    os.remove(thumb_fp)
                except Exception:
                    pass
            try:
                os.remove(filepath)
            except Exception:
                pass

            if sent_msg and "youtube" in source_url.lower():
                try:
                    await safe_edit(status_msg, "📝 Getting video info...")
                    info = await asyncio.wait_for(
                        extract_youtube_info(source_url), timeout=60
                    )
                    if isinstance(info, dict):
                        title = info.get("title", "")
                        desc = info.get("description", "")
                    else:
                        lines = info.split("\n")
                        clean_lines = [
                            l.strip()
                            for l in lines
                            if l.strip()
                            and l.strip()
                            not in ("Free Download", "TITLE & DESCRIPTION:", "---")
                        ]
                        title = clean_lines[0] if clean_lines else yt_title
                        desc = (
                            "\n".join(clean_lines[1:]).strip()
                            if len(clean_lines) > 1
                            else ""
                        )
                    extra = ""
                    if title:
                        extra += f"\n🎬 **{title}**"
                    if desc:
                        extra += f"\n📝 {desc}"
                    if extra:
                        new_caption = f"{caption_start}\n📦 {human_readable_size(final_size)}\n🔗 [Source]({source_url}){gh_line}{extra}"
                        try:
                            await event.client.edit_message(
                                event.chat_id, sent_msg.id, text=new_caption
                            )
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"[Y2MATE_EXTRACT] Error: {e}", exc_info=True)
                    try:
                        err_msg = str(e)[:200]
                        ss_b64 = getattr(e, "screenshot_b64", "")
                        if ss_b64:
                            await event.client.send_file(
                                event.chat_id,
                                base64.b64decode(ss_b64),
                                caption=f"⚠️ Extractor failed:\n{err_msg}",
                            )
                        else:
                            await event.client.send_message(
                                event.chat_id, f"⚠️ Extractor log: {err_msg}"
                            )
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception as e:
            await safe_edit(status_msg, f"❌ Upload failed: {str(e)[:100]}")
    except Exception as e:
        logger.error(f"[Y2MATE_CB] Error: {e}", exc_info=True)
        try:
            await event.edit(f"❌ Error: {str(e)[:100]}")
        except Exception:
            pass
        try:
            await session.close_browser()
        except Exception:
            pass
    raise events.StopPropagation


async def y2mate_cancel_callback(event):
    pick_id = event.data.decode()[5:]
    if pick_id in y2mate_sessions:
        entry = y2mate_sessions.pop(pick_id)
        try:
            await entry["session"].close_browser()
        except Exception:
            pass
    await event.answer("❌ Cancelled", alert=False)
    try:
        await event.edit("❌ Y2Mate cancelled.", buttons=None)
    except Exception:
        pass


async def savep_cancel_callback(event):
    session_id = event.data.decode().replace("savep_cancel_", "")
    cancelled = trigger_savep_cancel(session_id)
    await event.answer("🚫 Cancelling..." if cancelled else "Already done", alert=False)
    if cancelled:
        try:
            await event.edit("🚫 **Cancelled.**", buttons=None)
        except Exception:
            pass
async def main():
    print("\n" + "=" * 60)
    print("🚀 ULTIMATE BOT v5")
    print("   FIX 1: 403 → auto-retry via Dirpy")
    print("   FIX 2: FFmpeg -noautorotate + yuv420p")
    print("   FIX 3: size_input uses chat_id (not sender_id)")
    print("   FIX 4: pause/resume split callbacks")
    print("   FIX 5: command pattern conflict resolved")
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
        y2mate_quality_callback,
        events.CallbackQuery(pattern=r"y2m_(?!cancel)(.+)_(\d+)"),
    )
    client.add_event_handler(
        y2mate_quality_callback, events.CallbackQuery(pattern=r"y2mq_.+")
    )
    client.add_event_handler(
        y2mate_cancel_callback, events.CallbackQuery(pattern=r"y2mc_.+")
    )
    client.add_event_handler(
        savep_cancel_callback, events.CallbackQuery(pattern=r"savep_cancel_.+")
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
