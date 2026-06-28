"""
happyscribe_subtitle.py
-----------------------
آپلود ویدیو + زیرنویس به HappyScribe و دریافت لینک دانلود نهایی.

استفاده:
    from happyscribe_subtitle import hardcode_subtitle_online

    async def progress_cb(text: str):
        await status_msg.edit(text)

    download_url, error = await hardcode_subtitle_online(
        video_path="/tmp/video.mp4",
        subtitle_path="/tmp/sub.srt",
        progress_callback=progress_cb,
    )
    if error:
        print("Error:", error)
    else:
        print("Download URL:", download_url)
"""

import asyncio
import logging
import os
from typing import Callable, Coroutine, Optional, Tuple

from playwright.async_api import async_playwright, Page, Browser, TimeoutError as PlaywrightTimeout

logger = logging.getLogger("HappyScribe")

TOOL_URL = "https://www.happyscribe.com/tools/hardcode-subtitles-video"

# محدودیت سایز سایت (حدودی — سایت خودش چک میکنه)
MAX_FILE_SIZE_MB = 500


# ─────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────

ProgressCB = Optional[Callable[[str], Coroutine]]


async def _report(cb: ProgressCB, text: str):
    if cb:
        try:
            await cb(text)
        except Exception:
            pass
    logger.info(f"[HappyScribe] {text}")


async def _upload_file_via_label(page: Page, input_id: str, file_path: str):
    """
    فایل رو از طریق input[type=file] آپلود میکنه.
    label کلیک نمیکنه — مستقیم set_input_files روی input میزنه.
    """
    file_input = page.locator(f"input#{input_id}")
    await file_input.set_input_files(file_path)


async def _wait_for_upload_percent(
    page: Page,
    cb: ProgressCB,
    timeout_s: int = 600,
):
    """
    درصد آپلود رو از label-submit میخونه و به callback گزارش میده.
    وقتی label دیگه 'Uploading' نداشت، تموم شده.
    """
    label = page.locator("#label-submit")
    interval = 2  # ثانیه
    elapsed = 0
    last_text = ""

    while elapsed < timeout_s:
        try:
            text = (await label.inner_text(timeout=5000)).strip()
        except Exception:
            text = ""

        # وقتی متن عوض شد گزارش بده
        if text and text != last_text:
            last_text = text
            if "Uploading" in text or "%" in text:
                await _report(cb, f"☁️ {text}")
            elif "Hardcode" in text and "processing" not in text.lower():
                # آپلود تموم شد — دکمه فعال شد
                break

        # اگه label class داره hs-status processing → هنوز آپلود میکنه
        cls = await label.get_attribute("class") or ""
        if "processing" in cls:
            await asyncio.sleep(interval)
            elapsed += interval
            continue
        else:
            break

    await _report(cb, "✅ Upload complete, processing started...")


async def _wait_for_processing(
    page: Page,
    cb: ProgressCB,
    timeout_s: int = 1800,  # حداکثر ۳۰ دقیقه
) -> str:
    """
    صبر میکنه تا صفحه processing تموم بشه و لینک دانلود ظاهر بشه.
    درصد رو از #progress-percent میخونه.
    برمیگردونه: download_url یا "" در صورت خطا.
    """
    interval = 3
    elapsed = 0
    last_pct = ""

    while elapsed < timeout_s:
        # چک لینک دانلود
        dl_link = page.locator("#download-link")
        try:
            href = await dl_link.get_attribute("href", timeout=2000)
            if href and href.startswith("http"):
                await _report(cb, "✅ Processing complete! Downloading...")
                return href
        except Exception:
            pass

        # چک درصد پردازش
        pct_el = page.locator("#progress-percent")
        try:
            pct = (await pct_el.inner_text(timeout=2000)).strip()
            if pct and pct != last_pct:
                last_pct = pct
                await _report(cb, f"⚙️ Processing: {pct}")
        except Exception:
            pass

        await asyncio.sleep(interval)
        elapsed += interval

    return ""


# ─────────────────────────────────────────────
# تابع اصلی
# ─────────────────────────────────────────────

async def hardcode_subtitle_online(
    video_path: str,
    subtitle_path: str,
    progress_callback: ProgressCB = None,
) -> Tuple[str, str]:
    """
    ویدیو و زیرنویس رو روی HappyScribe آپلود میکنه و لینک دانلود میده.

    Returns:
        (download_url, error_message)
        موفق: (url, "")
        خطا:  ("", error_msg)
    """
    # بررسی فایل‌ها
    for path, label in ((video_path, "Video"), (subtitle_path, "Subtitle")):
        if not os.path.exists(path):
            return "", f"{label} file not found: {path}"
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb > MAX_FILE_SIZE_MB:
            return "", f"{label} file too large ({size_mb:.0f} MB > {MAX_FILE_SIZE_MB} MB limit)"

    browser: Optional[Browser] = None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                accept_downloads=True,
            )
            page = await context.new_page()

            # ── ۱. باز کردن صفحه ──────────────────────────────────────
            await _report(progress_callback, "🌐 Opening HappyScribe...")
            await page.goto(TOOL_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # ── ۲. آپلود ویدیو ─────────────────────────────────────────
            await _report(progress_callback, "📹 Uploading video file...")
            try:
                await _upload_file_via_label(page, "video", video_path)
            except Exception as e:
                return "", f"Failed to upload video: {e}"

            await page.wait_for_timeout(1500)

            # ── ۳. آپلود زیرنویس ──────────────────────────────────────
            await _report(progress_callback, "🔤 Uploading subtitle file...")
            try:
                await _upload_file_via_label(page, "subtitles", subtitle_path)
            except Exception as e:
                return "", f"Failed to upload subtitle: {e}"

            await page.wait_for_timeout(1500)

            # ── ۴. کلیک روی Hardcode Subtitles ────────────────────────
            await _report(progress_callback, "🚀 Starting hardcode process...")
            try:
                submit_label = page.locator("#label-submit")
                await submit_label.click(timeout=10000)
            except Exception as e:
                return "", f"Failed to click submit: {e}"

            # ── ۵. گزارش درصد آپلود ────────────────────────────────────
            await _wait_for_upload_percent(page, progress_callback, timeout_s=600)

            # ── ۶. صبر برای ریدایرکت به صفحه processing ──────────────
            await _report(progress_callback, "⏳ Waiting for processing page...")
            try:
                await page.wait_for_url(
                    "**/tools/hardcode-subtitles-video/**",
                    timeout=120_000,
                )
            except PlaywrightTimeout:
                # شاید URL عوض نشه — ولی همونجا process بشه
                pass

            await page.wait_for_timeout(2000)

            # ── ۷. گزارش درصد پردازش و انتظار برای لینک دانلود ─────────
            download_url = await _wait_for_processing(
                page, progress_callback, timeout_s=1800
            )

            if not download_url:
                # یه بار دیگه چک کن
                try:
                    dl = page.locator("#download-link")
                    href = await dl.get_attribute("href", timeout=5000)
                    if href and href.startswith("http"):
                        download_url = href
                except Exception:
                    pass

            if not download_url:
                return "", "Timed out waiting for download link."

            return download_url, ""

    except PlaywrightTimeout as e:
        return "", f"Page timed out: {str(e)[:120]}"
    except Exception as e:
        logger.error(f"[HappyScribe] Unexpected error: {e}", exc_info=True)
        return "", f"Unexpected error: {str(e)[:150]}"
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


# ─────────────────────────────────────────────
# تست مستقل
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python happyscribe_subtitle.py <video_path> <subtitle_path>")
        sys.exit(1)

    async def _cb(text):
        print(text)

    url, err = asyncio.run(
        hardcode_subtitle_online(sys.argv[1], sys.argv[2], _cb)
    )
    if err:
        print(f"ERROR: {err}")
    else:
        print(f"Download URL: {url}")
