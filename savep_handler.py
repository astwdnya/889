import asyncio
import logging
import os
import time

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

API_BASE = "https://api.v02.savethevideo.com"


async def _download_file(url, filepath, progress_cb):
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.savethevideo.com/",
    }
    try:
        timeout = ClientTimeout(connect=30, sock_read=120, total=1800)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    return False, f"HTTP {resp.status}", 0
                content_length = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                start_time = time.time()
                last_update = 0
                async with aiofiles.open(filepath, "wb") as f:
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
                                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                                text = (
                                    f"📥 **Downloading...**\n`[{bar}]`\n"
                                    f"💾 {downloaded / 1024 / 1024:.1f}/{content_length / 1024 / 1024:.1f} MB"
                                    f"  •  ⚡ {speed / 1024 / 1024:.1f} MB/s\n📊 {pct:.1f}%"
                                )
                            else:
                                text = (
                                    f"📥 **Downloading...**\n"
                                    f"💾 {downloaded / 1024 / 1024:.1f} MB  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
                                )
                            await progress_cb(text)
                return True, None, os.path.getsize(filepath)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[SAVEP] Download error: {e}")
        return False, str(e)[:150], 0


async def _call_api(method, path, data=None, headers=None):
    url = API_BASE + path
    req_headers = {
        "Accept": "application/json",
        "Origin": "https://www.savethevideo.com",
        "Referer": "https://www.savethevideo.com/",
        "User-Agent": _USER_AGENT,
    }
    if headers:
        req_headers.update(headers)

    try:
        timeout = ClientTimeout(connect=15, sock_read=30, total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if method == "POST":
                req_headers["Content-Type"] = "application/json"
                async with session.post(url, json=data, headers=req_headers) as resp:
                    body = await resp.json()
                    if resp.status >= 400:
                        raise Exception(
                            f"API error {resp.status}: {body.get('message', body)}"
                        )
                    return body
            else:
                async with session.get(url, headers=req_headers) as resp:
                    body = await resp.json()
                    if resp.status >= 400:
                        raise Exception(
                            f"API error {resp.status}: {body.get('message', body)}"
                        )
                    return body
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[SAVEP] API call error: {e}")
        raise


def _extract_url_from_result(task):
    """Extract the best download URL from the API response.
    Response structure can be:
    - result is an array of video items, each with url and formats
    - Or result is an object with formats/files/entries
    """
    result = task.get("result", task)
    if isinstance(result, list):
        for item in result:
            # Prefer mp4 URL
            url = item.get("url", "") or ""
            if url and (".mp4" in url or "googlevideo" in url):
                return url
            # Check sub-formats
            formats = item.get("formats", [])
            for fmt in formats:
                fu = fmt.get("url", "") or ""
                if fu and (".mp4" in fu or "googlevideo" in fu):
                    return fu
            # Return first URL found
            if url:
                return url
            for fmt in formats:
                fu = fmt.get("url", "") or ""
                if fu:
                    return fu
        return None

    if isinstance(result, dict):
        formats = result.get("formats", [])
        if formats:
            for fmt in formats:
                url = fmt.get("url", "") or ""
                if url and (".mp4" in url or "googlevideo" in url):
                    return url
            return formats[0].get("url", "")
        url = result.get("url", "") or ""
        if url:
            return url
        files = result.get("files", [])
        if files:
            return files[0] if isinstance(files[0], str) else files[0].get("url", "")
        entries = result.get("entries", [])
        if entries:
            for entry in entries:
                eu = entry.get("url", "") or ""
                if eu:
                    return eu

    return None


async def _create_task(video_url):
    body = await _call_api("POST", "/tasks", data={"type": "info", "url": video_url})
    return body


async def _poll_task(task_href, progress_cb, stop_event=None):
    interval = 1.5
    timeout = 900
    start = time.time()
    last_status = ""

    while True:
        if stop_event is not None and stop_event.is_set():
            return None

        elapsed = time.time() - start
        if elapsed > timeout:
            raise Exception("Task polling timed out")

        body = await _call_api("GET", task_href)

        state = body.get("state", "")
        if state == "completed":
            return _extract_url_from_result(body)

        elif state == "failed":
            error = body.get("error", {})
            code = error.get("code", "unknown")
            msg = error.get("message", "Unknown error")
            raise Exception(f"Task failed: code={code}, msg={msg}")

        elif state == "progress" or state == "pending":
            status_text = body.get("statusText", "") or body.get("message", "") or ""
            if status_text and status_text != last_status:
                last_status = status_text
                progress_cb(f"⚙️ {status_text}")

        await asyncio.sleep(interval)


async def _async_extract_savep_v2(video_url, progress_cb, stop_event=None):
    """Extract video download URL from savethevideo.com using direct API calls."""
    try:
        progress_cb("🌐 Creating task on savethevideo.com...")
        task = await _create_task(video_url)
        logger.info(f"[SAVEP] Task response: {task}")

        task_href = task.get("href", "")
        task_state = task.get("state", "")

        progress_cb(f"✅ Task created (state: {task_state})")

        # Extract download URL from response
        dl_url = _extract_url_from_result(task)
        if dl_url:
            return [dl_url]

        if task_state == "completed":
            return ["❌ No download URL found in completed task"]

        if not task_href:
            return [f"ERROR: No task href in response"]

        progress_cb("⏳ Polling for results...")
        dl_url = await _poll_task(task_href, progress_cb, stop_event)

        if dl_url:
            progress_cb("✅ Download URL obtained!")
            return [dl_url]
        else:
            # Fallback: try creating a download task with format=mp4
            progress_cb("🔄 Trying download task with mp4 format...")
            dl_task = await _call_api(
                "POST",
                "/tasks",
                data={"type": "download", "url": video_url, "format": "mp4"},
            )
            dl_href = dl_task.get("href", "")
            if dl_href:
                dl_url2 = await _poll_task(dl_href, progress_cb, stop_event)
                if dl_url2:
                    return [dl_url2]
            return ["❌ Could not obtain download URL from API"]

    except Exception as e:
        progress_cb(f"❌ API error: {str(e)[:200]}")
        logger.error(f"[SAVEP] API extraction error: {e}", exc_info=True)
        return [f"ERROR: {str(e)[:250]}"]


async def process_savep_request(event, url, safe_edit_fn, send_file_fn, download_dir):
    status_msg = await event.reply("🔄 Starting extraction...", parse_mode="markdown")

    async def update_status(text):
        try:
            await safe_edit_fn(status_msg, text)
        except Exception:
            pass

    progress_log = []

    def sync_progress_cb(msg):
        progress_log.append(msg)
        logger.info(f"[SAVEP] {msg}")

    async def live_progress_loop():
        while True:
            await asyncio.sleep(4)
            if progress_log:
                text = (
                    "🔄 **Extracting...**\n```\n"
                    + "\n".join(progress_log[-4:])
                    + "\n```"
                )
                try:
                    await safe_edit_fn(status_msg, text)
                except Exception:
                    pass

    progress_task = asyncio.create_task(live_progress_loop())

    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        await update_status("🌐 **Connecting to savethevideo.com...**")
        links = await _async_extract_savep_v2(url, sync_progress_cb)
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    if not links or links[0].startswith("❌") or links[0].startswith("ERROR"):
        err_detail = links[0] if links else "No links found"
        log_text = "\n".join(progress_log[-6:]) if progress_log else "No log"
        await update_status(
            f"❌ **Extraction failed**\n`{err_detail}`\n\n**Last log:**\n```\n{log_text}\n```"
        )
        return

    direct_url = links[0]
    logger.info(f"[SAVEP] Got link: {direct_url[:120]}")
    await update_status("✅ **Link found!**\n\n📥 Starting download...")

    filename = f"savep_{event.chat_id}_{int(time.time())}.mp4"
    filepath = os.path.join(download_dir, filename)

    async def progress_text_cb(text):
        try:
            await safe_edit_fn(status_msg, text)
        except Exception:
            pass

    success, dl_error, final_size = await _download_file(
        direct_url, filepath, progress_text_cb
    )

    if not success or not os.path.exists(filepath):
        await update_status(f"❌ **Download failed:** `{dl_error}`")
        return
    if final_size < 1024:
        await update_status(f"❌ **Download failed:** File too small ({final_size}B)")
        try:
            os.remove(filepath)
        except Exception:
            pass
        return

    await update_status("📤 **Uploading video...**")
    try:
        caption = (
            f"🎬 **Video Downloaded**\n"
            f"📦 Size: `{final_size / 1024 / 1024:.1f} MB`\n"
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
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
