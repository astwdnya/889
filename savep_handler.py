"""
savep_handler.py — هندلر دستور /savep برای ربات
از savep.py برای استخراج لینک دانلود استفاده میکنه
سپس فایل رو دانلود و مستقیم برای کاربر ارسال میکنه
"""

import asyncio
import logging
import os
import time
import threading

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)


# ── دانلود فایل با نمایش progress ─────────────────────────────────────────

async def _download_file(url: str, filepath: str, status_msg, progress_cb_text) -> tuple:
    """
    فایل رو از url دانلود میکنه و در filepath ذخیره میکنه.
    returns: (success: bool, error_msg: str | None, file_size: int)
    """
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Referer': 'https://www.savethevideo.com/',
    }

    try:
        timeout = ClientTimeout(connect=30, sock_read=120, total=1800)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}", 0

                content_length = int(resp.headers.get('Content-Length', 0))
                downloaded = 0
                start_time = time.time()
                last_update = 0

                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(512 * 1024):
                        await f.write(chunk)
                        downloaded += len(chunk)

                        now = time.time()
                        if now - last_update >= 2.0:
                            last_update = now
                            elapsed = now - start_time
                            speed = downloaded / elapsed if elapsed > 0 else 0

                            if content_length > 0:
                                pct = downloaded / content_length * 100
                                bar_filled = int(pct / 5)
                                bar = "█" * bar_filled + "░" * (20 - bar_filled)
                                size_str = f"{downloaded / 1024 / 1024:.1f}/{content_length / 1024 / 1024:.1f} MB"
                                speed_str = f"{speed / 1024 / 1024:.1f} MB/s"
                                text = (
                                    f"📥 **Downloading...**\n"
                                    f"`[{bar}]`\n"
                                    f"💾 {size_str}  •  ⚡ {speed_str}\n"
                                    f"📊 {pct:.1f}%"
                                )
                            else:
                                size_str = f"{downloaded / 1024 / 1024:.1f} MB"
                                speed_str = f"{speed / 1024 / 1024:.1f} MB/s"
                                text = (
                                    f"📥 **Downloading...**\n"
                                    f"💾 {size_str}  •  ⚡ {speed_str}"
                                )
                            await progress_cb_text(text)

                final_size = os.path.getsize(filepath)
                return True, None, final_size

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[SAVEP] Download error: {e}")
        return False, str(e)[:150], 0


# ── هندلر اصلی /savep ─────────────────────────────────────────────────────

async def process_savep_request(event, url: str, safe_edit_fn, send_file_fn,
                                 download_dir: str = "/tmp"):
    """
    هندلر کامل /savep:
    1. savep.py رو صدا میزنه تا لینک دانلود رو پیدا کنه
    2. فایل رو دانلود میکنه
    3. ویدیو رو برای کاربر میفرسته

    پارامترها:
        event           — telethon event
        url             — آدرس ویدیو
        safe_edit_fn    — تابع safe_edit(msg, text) از bot.py
        send_file_fn    — تابع send_file_with_progress از bot.py
        download_dir    — پوشه‌ای که فایل موقت توش ذخیره میشه
    """
    from savep import run_savep_extract_sync, _is_youtube_url, run_ytdown_extract_sync

    status_msg = await event.reply("🔄 Starting SaveTheVideo extraction...", parse_mode='markdown')

    # ── تابع کمکی برای آپدیت پیام status ─────────────────────────────────
    async def update_status(text: str):
        try:
            await safe_edit_fn(status_msg, text)
        except Exception:
            pass

    # ── جمع‌آوری لاگ‌های progress از thread جداگانه ──────────────────────
    progress_log: list[str] = []
    last_sent_log = [0.0]

    def sync_progress_cb(msg: str):
        progress_log.append(msg)
        logger.info(f"[SAVEP] {msg}")

    # ── پیام‌های live progress روی تلگرام ────────────────────────────────
    async def live_progress_loop():
        """هر ۴ ثانیه آخرین log ها رو نشون میده"""
        while True:
            await asyncio.sleep(4)
            if progress_log:
                last_lines = progress_log[-4:]
                text = "🔄 **Extracting...**\n```\n" + "\n".join(last_lines) + "\n```"
                try:
                    await safe_edit_fn(status_msg, text)
                except Exception:
                    pass

    progress_task = asyncio.create_task(live_progress_loop())

    try:
        await update_status("🌐 **Opening savethevideo.com...**\n_This may take 30-60 seconds_")

        # ── اجرا در thread جداگانه چون synchronous ───────────────────────
        loop = asyncio.get_event_loop()

        if _is_youtube_url(url):
            sync_progress_cb("🎬 YouTube URL detected — using ytdown.to")
            links = await loop.run_in_executor(
                None,
                lambda: run_ytdown_extract_sync(url, sync_progress_cb)
            )
        else:
            links = await loop.run_in_executor(
                None,
                lambda: run_savep_extract_sync(url, sync_progress_cb, direct_download=False)
            )

    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    # ── بررسی نتیجه extraction ────────────────────────────────────────────
    if not links or links[0].startswith("❌") or links[0].startswith("ERROR"):
        err_detail = links[0] if links else "No links found"
        log_text = "\n".join(progress_log[-6:]) if progress_log else "No log"
        await update_status(
            f"❌ **Extraction failed**\n`{err_detail}`\n\n"
            f"**Last log:**\n```\n{log_text}\n```"
        )
        return

    direct_url = links[0]
    logger.info(f"[SAVEP] Got link: {direct_url[:120]}")

    await update_status(
        f"✅ **Link found!**\n"
        f"⬇️ `{direct_url[:80]}...`\n\n"
        f"📥 Starting download..."
    )

    # ── دانلود فایل ───────────────────────────────────────────────────────
    filename = f"savep_{event.chat_id}_{int(time.time())}.mp4"
    filepath = os.path.join(download_dir, filename)

    async def progress_text_cb(text: str):
        try:
            await safe_edit_fn(status_msg, text)
        except Exception:
            pass

    success, dl_error, final_size = await _download_file(
        direct_url, filepath, status_msg, progress_text_cb
    )

    if not success or not os.path.exists(filepath):
        await update_status(f"❌ **Download failed:** `{dl_error}`")
        return

    if final_size == 0:
        await update_status("❌ **Download failed:** File is empty")
        try:
            os.remove(filepath)
        except Exception:
            pass
        return

    # ── ارسال ویدیو ───────────────────────────────────────────────────────
    await update_status("📤 **Uploading video...**")

    try:
        from telethon.tl.types import DocumentAttributeVideo

        size_mb = final_size / 1024 / 1024
        caption = (
            f"🎬 **Video Downloaded**\n"
            f"📦 Size: `{size_mb:.1f} MB`\n"
            f"🔗 [Source]({url})\n"
            f"⬇️ [DW Link]({direct_url})"
        )

        await send_file_fn(
            client=event.client,
            chat_id=event.chat_id,
            filepath=filepath,
            caption=caption,
            status_msg=status_msg,
            buttons=None,
            supports_streaming=True,
        )

    except Exception as e:
        logger.error(f"[SAVEP] Upload error: {e}", exc_info=True)
        await update_status(f"❌ **Upload failed:** `{str(e)[:120]}`")

    finally:
        # پاک کردن فایل موقت
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info(f"[SAVEP] Temp file deleted: {filepath}")
        except Exception as e:
            logger.warning(f"[SAVEP] Could not delete temp file: {e}")
