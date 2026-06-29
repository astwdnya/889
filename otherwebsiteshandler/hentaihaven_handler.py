"""
hentaihaven_handler.py
----------------------
استخراج لینک‌های دانلود از hentaihaven.xxx و ارسال ویدیو به کاربر.

روش کار (player-logic API):
  1. صفحه /watch/ رو می‌گیریم و player.php?data=... تازه رو استخراج می‌کنیم
  2. player.php یه x-secure-token می‌ده که با (ROT13 → atob) ×3 decode میشه
  3. توکن decode شده یه JSON config داره با en/iv/uri
  4. api.php با action=zarat_get_data_player_ajax صدا زده میشه
  5. پاسخ شامل لینک master.m3u8 هست
  6. دانلود با yt-dlp (m3u8) یا curl_cffi/aiohttp (direct)
"""

import asyncio
import base64
import codecs
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

import aiofiles
import aiohttp
from aiohttp import ClientTimeout

logger = logging.getLogger("HentaiHavenHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
SESSION_TTL = 30 * 60
MAX_RETRIES = 3
RETRY_DELAY = 2.0
MAX_EMBED_PROBE = 8

_SITE_DOMAIN = "hentaihaven.xxx"
_SITE_URL = "https://hentaihaven.xxx"
_SITE_REFERER = f"{_SITE_URL}/"
_PLAYER_LOGIC_PATH = "/wp-content/plugins/player-logic/"

_ALLOWED_HOSTS = frozenset({
    "hentaihaven.xxx",
    "www.hentaihaven.xxx",
})

_ALLOWED_HOST_SUFFIXES = (
    ".hentaihaven.xxx",
    ".master-lengs.org",
    ".cdntrex.com",
    ".kvcdn.com",
    ".cdn13.com",
    ".betacdn.net",
    ".bcdn.cc",
    ".gvideo.io",
    ".googleapis.com",
    ".googleusercontent.com",
    ".fastly.net",
    ".cloudfront.net",
    ".akamaized.net",
    ".hwcdn.net",
    ".streamtape.com",
    ".stape.fun",
    ".doodstream.com",
    ".dood.to",
    ".dood.so",
    ".dood.watch",
    ".mp4upload.com",
    ".sendvid.com",
    ".fembed.com",
    ".mixdrop.co",
    ".mixdrop.to",
    ".upstream.to",
    ".streamsb.net",
    ".vidoza.net",
    ".filemoon.sx",
    ".filemoon.to",
    ".voe.sx",
    ".streamwish.to",
    ".streamwish.com",
    ".vidhide.com",
    ".vidguard.to",
    ".lulustream.com",
    ".emturbovid.com",
    ".turbovid.com",
)

_SKIP_EXTENSIONS = frozenset({
    ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map",
    ".json", ".xml", ".txt", ".html", ".htm", ".php",
})

ProgressCallback = Callable[[str], Awaitable[None]]

hentaihaven_sessions: dict = {}


# ─── Utility ────────────────────────────────────────────────


def is_hentaihaven_url(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return host in _ALLOWED_HOSTS or host.endswith(f".{_SITE_DOMAIN}")
    except Exception:
        return False


def _is_allowed_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname or ""
        return (
            host in _ALLOWED_HOSTS
            or any(host.endswith(s) for s in _ALLOWED_HOST_SUFFIXES)
        )
    except Exception:
        return False


def cleanup_expired_sessions() -> int:
    now = time.time()
    expired = [
        sid for sid, data in hentaihaven_sessions.items()
        if now - data.get("created_at", 0) > SESSION_TTL
    ]
    for sid in expired:
        hentaihaven_sessions.pop(sid, None)
    if expired:
        logger.info("Cleaned up %d expired hentaihaven sessions", len(expired))
    return len(expired)


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _normalize_url(url: str, base_url: str = "") -> Optional[str]:
    url = url.replace("\\/", "/").strip()
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http") and base_url:
        url = urljoin(base_url, url)
    elif not url.startswith("http"):
        return None
    return url


def _quality_sort_key(q: dict) -> int:
    nums = re.findall(r"\d+", q["label"])
    return int(nums[-1]) if nums else 0


def _format_progress(
    downloaded: int, content_length: int, start_time: float, now: float,
) -> str:
    elapsed = now - start_time
    speed = downloaded / elapsed if elapsed > 0 else 0
    dl_mb = downloaded / 1024 / 1024
    if content_length > 0:
        total_mb = content_length / 1024 / 1024
        pct = downloaded / content_length * 100
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        return (
            f"📥 **Downloading...**\n`[{bar}]`\n"
            f"💾 {dl_mb:.1f}/{total_mb:.1f} MB"
            f"  •  ⚡ {speed / 1024 / 1024:.1f} MB/s\n📊 {pct:.1f}%"
        )
    return (
        f"📥 **Downloading...**\n💾 {dl_mb:.1f} MB"
        f"  •  ⚡ {speed / 1024 / 1024:.1f} MB/s"
    )


def _check_impersonation_support() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


def _find_output_file(filepath: str) -> Optional[str]:
    if os.path.exists(filepath):
        return filepath
    base, _ = os.path.splitext(filepath)
    for ext in (".mp4", ".mkv", ".webm", ".ts"):
        candidate = base + ext
        if os.path.exists(candidate):
            try:
                os.rename(candidate, filepath)
                return filepath
            except OSError:
                return candidate
    return None


def _is_valid_video_content(filepath: str) -> bool:
    """بررسی اینکه فایل دانلود شده واقعاً ویدیو هست نه JS/HTML."""
    try:
        if not os.path.exists(filepath):
            return False
        size = os.path.getsize(filepath)
        if size == 0:
            return False

        with open(filepath, "rb") as f:
            header = f.read(512)

        # magic bytes ویدیو - همیشه چک میشه
        if b"ftyp" in header[:16]:           # MP4/MOV
            return True
        if header[:4] == b"\x1a\x45\xdf\xa3":  # WebM/Matroska (EBML)
            return True
        if header[:1] == b"\x47":            # MPEG-TS
            return True
        if header[:3] == b"FLV":             # FLV
            return True

        # محتوای متنی/HTML/JS → ویدیو نیست
        text = header.decode("utf-8", errors="ignore").lower()
        if any(kw in text for kw in [
            "<!doctype", "<html", "<script", "function(",
            "{\"status\"", "jquery",
        ]):
            logger.warning("Downloaded file looks like text/JS, not video")
            return False

        # فایل‌های بزرگ بدون magic شناخته‌شده ولی بدون نشانه‌ی متن:
        # احتمالاً m3u8-merged یا فرمت دیگه - قبول با اطمینان کمتر
        if size >= 100 * 1024:
            return True

        # فایل کوچیک و بدون magic → مشکوک
        return False
    except Exception:
        # fail-safe: در صورت خطا، نامعتبر فرض می‌کنیم
        return False


# ─── HTTP helpers ───────────────────────────────────────────


@asynccontextmanager
async def _get_session(timeout: Optional[ClientTimeout] = None):
    t = timeout or ClientTimeout(total=30, connect=10)
    jar = aiohttp.CookieJar()
    session = aiohttp.ClientSession(
        timeout=t, headers=_DEFAULT_HEADERS, cookie_jar=jar
    )
    try:
        yield session
    finally:
        await session.close()


def _extract_title(html: str) -> str:
    m = re.search(
        r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:title["\']',
            html, re.IGNORECASE,
        )
    if m:
        return m.group(1).strip()

    m = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        title = re.sub(r"\s*[-|]\s*[Hh]entai\s*[Hh]aven.*$", "", title).strip()
        return title or "Untitled"

    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return "Untitled"


# ─── player-logic token decode ──────────────────────────────


def _rot13(s: str) -> str:
    return codecs.encode(s, "rot_13")


def _safe_b64(s: str) -> str:
    s = s.strip()
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    return base64.b64decode(s).decode("utf-8")


def _decode_player_token(raw_token: str) -> Optional[dict]:
    """
    decode الگوریتم player-logic:
      sha512- حذف → (ROT13 → atob) ×3 → JSON.parse
    """
    try:
        val = raw_token.replace("sha512-", "")
        for _ in range(3):
            val = _safe_b64(_rot13(val))
        return json.loads(val)
    except Exception as e:
        logger.warning("Player token decode failed: %s", e)
        return None


# ─── Main extraction ───────────────────────────────────────


async def extract_hentaihaven_qualities(url: str) -> Tuple[List[dict], str]:
    """
    لینک ویدیو رو از صفحه hentaihaven استخراج میکنه.

    فلو:
      watch page → player.php?data= → x-secure-token
      → decode (ROT13/atob ×3) → config{en,iv,uri}
      → api.php → sources[].src (m3u8)
    """
    if not is_hentaihaven_url(url):
        return [], "Invalid URL"

    cleanup_expired_sessions()

    if not _check_impersonation_support():
        return [], "curl_cffi لازمه. نصب: pip install curl_cffi"

    from curl_cffi.requests import AsyncSession

    try:
        async with AsyncSession() as session:
            # warmup: کوکی Cloudflare
            try:
                await session.get(_SITE_REFERER, impersonate="chrome", timeout=15)
            except Exception:
                pass

            # 1. watch page → data تازه
            logger.info("Fetching watch page: %s", url)
            wr = await session.get(
                url, impersonate="chrome",
                headers={"Referer": _SITE_REFERER}, timeout=20,
            )
            if wr.status_code != 200:
                return [], f"watch page HTTP {wr.status_code}"

            title = _extract_title(wr.text)

            m = re.search(
                r'player\.php\?data=([A-Za-z0-9+/=_-]+)', wr.text
            )
            if not m:
                return [], "player data not found in page"
            data_param = m.group(1)

            # 2. player.php → x-secure-token
            player_url = (
                f"{_SITE_URL}{_PLAYER_LOGIC_PATH}player.php?data={data_param}"
            )
            pr = await session.get(
                player_url, impersonate="chrome",
                headers={"Referer": url}, timeout=20,
            )
            if pr.status_code != 200:
                return [], f"player.php HTTP {pr.status_code}"

            tm = re.search(
                r'x-secure-token["\']?\s+content=["\']([^"\']+)["\']',
                pr.text, re.IGNORECASE,
            )
            if not tm:
                return [], "secure token not found"

            # 3. decode token → config
            config = _decode_player_token(tm.group(1))
            if not config:
                return [], "token decode failed"

            en = config.get("en", "")
            iv = config.get("iv", "")
            uri = config.get("uri", "")
            if uri.startswith("//"):
                uri = "https:" + uri
            if not uri:
                uri = f"{_SITE_URL}{_PLAYER_LOGIC_PATH}"
            if not en:
                return [], "encrypted payload (en) missing in config"

            # 4. api.php → sources
            api_url = f"{uri}api.php"
            api_resp = await session.post(
                api_url,
                data={
                    "action": "zarat_get_data_player_ajax",
                    "a": en,
                    "b": iv,
                },
                impersonate="chrome",
                headers={
                    "Referer": player_url,
                    "Origin": _SITE_URL,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=20,
            )
            if api_resp.status_code != 200:
                return [], f"api.php HTTP {api_resp.status_code}"

            try:
                api_data = json.loads(api_resp.text)
            except json.JSONDecodeError:
                return [], "api.php returned invalid JSON"

            if not api_data.get("status"):
                return [], f"api error: {api_data.get('error', 'unknown')}"

            sources = api_data.get("data", {}).get("sources", [])
            qualities = _parse_api_sources(sources)

            if qualities:
                qualities.sort(key=_quality_sort_key, reverse=True)
                logger.info(
                    "Extracted %d sources for: %s", len(qualities), title[:60]
                )
                return qualities, title

            return [], "no playable sources in api response"

    except Exception as e:
        logger.warning("Extraction failed for %s: %s", url, e)
        return [], str(e)[:150]


def _parse_api_sources(sources: list) -> List[dict]:
    """تبدیل sources پاسخ api.php به لیست کیفیت."""
    qualities: List[dict] = []
    seen = set()

    for src in sources:
        if not isinstance(src, dict):
            continue
        src_url = (src.get("src") or "").replace("\\/", "/")
        if not src_url or src_url in seen:
            continue
        if not _is_allowed_host(src_url):
            logger.debug("Skipping non-allowed host: %s", src_url[:60])
            continue
        seen.add(src_url)

        label = src.get("label") or "Auto"
        src_type = (src.get("type") or "").lower()
        is_m3u8 = ".m3u8" in src_url or "mpegurl" in src_type

        qualities.append({
            "label": f"📡 {label}" if is_m3u8 else f"🎥 {label}",
            "url": src_url,
            "method": "m3u8" if is_m3u8 else "direct",
        })

    return qualities


# ─── Download: curl_cffi ────────────────────────────────────


async def _download_with_curl_cffi(
    url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        return False, "curl_cffi not installed", 0

    try:
        await progress_cb("📥 **شروع دانلود...**")
        async with AsyncSession() as session:
            resp = await session.get(
                url, impersonate="chrome",
                headers={"Referer": _SITE_REFERER, "Accept": "*/*"},
                allow_redirects=True, timeout=600, stream=True,
            )
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}", 0

            content_length = int(resp.headers.get("Content-Length", 0))
            if content_length > MAX_DOWNLOAD_SIZE:
                return False, f"File too large: {content_length / 1024 / 1024:.0f} MB", 0

            ct = resp.headers.get("Content-Type", "").lower()
            if any(t in ct for t in
                   ["text/html", "text/javascript", "application/javascript"]):
                logger.warning("Content-Type is %s, not video", ct)
                return False, "Response is not a video file", 0

            downloaded = 0
            start_time = time.time()
            last_update = 0.0

            async with aiofiles.open(filepath, "wb") as f:
                async for chunk in resp.aiter_content():
                    if not chunk:
                        continue
                    await f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_SIZE:
                        _cleanup_file(filepath)
                        return False, "Download exceeded size limit", 0
                    now = time.time()
                    if now - last_update >= 2.0:
                        last_update = now
                        await progress_cb(
                            _format_progress(downloaded, content_length, start_time, now)
                        )

        if not os.path.exists(filepath):
            return False, "File not created", 0
        if not _is_valid_video_content(filepath):
            _cleanup_file(filepath)
            return False, "Downloaded file is not a valid video", 0

        size = os.path.getsize(filepath)
        if size == 0:
            _cleanup_file(filepath)
            return False, "Downloaded file is empty", 0

        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        logger.warning("curl_cffi download error: %s", e)
        return False, str(e)[:150], 0


# ─── Download: yt-dlp ───────────────────────────────────────


async def _download_with_ytdlp(
    url: str, filepath: str, progress_cb: ProgressCallback,
    referer: Optional[str] = None,
) -> Tuple[bool, str, int]:
    if not shutil.which("yt-dlp"):
        return False, "yt-dlp not installed", 0

    await progress_cb("📥 **شروع دانلود (yt-dlp)...**")
    ref = referer or _SITE_REFERER
    try:
        cmd = [
            "yt-dlp", "--no-warnings", "--progress", "--newline",
            "--no-check-certificates", "-f", "best",
            "--max-filesize", str(MAX_DOWNLOAD_SIZE),
            "--add-header", f"Referer:{ref}",
            "--add-header", f"User-Agent:{_USER_AGENT}",
            "-o", filepath,
        ]
        if _check_impersonation_support():
            cmd.extend(["--impersonate", "chrome"])
        cmd.append(url)

        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        last_update = 0.0
        while True:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=120
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                _cleanup_file(filepath)
                return False, "Download timed out", 0
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

        actual_path = _find_output_file(filepath)
        if not actual_path:
            return False, "Output file not found", 0
        if not _is_valid_video_content(actual_path):
            _cleanup_file(actual_path)
            return False, "Downloaded file is not a valid video", 0

        size = os.path.getsize(actual_path)
        if size > MAX_DOWNLOAD_SIZE:
            _cleanup_file(actual_path)
            return False, "File exceeds size limit", 0
        return True, "", size

    except asyncio.CancelledError:
        _cleanup_file(filepath)
        raise
    except Exception as e:
        return False, str(e)[:150], 0


# ─── Download: aiohttp ──────────────────────────────────────


async def _download_with_aiohttp(
    url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    headers = {**_DEFAULT_HEADERS, "Referer": _SITE_REFERER}
    error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = ClientTimeout(total=3600, connect=30, sock_read=120)
            async with _get_session(timeout) as session:
                async with session.get(
                    url, headers=headers, allow_redirects=True
                ) as resp:
                    if resp.status != 200:
                        error = f"HTTP {resp.status}"
                        if resp.status != 403 and 400 <= resp.status < 500:
                            return False, error, 0
                        continue

                    ct = resp.headers.get("Content-Type", "").lower()
                    if any(t in ct for t in
                           ["text/html", "text/javascript", "application/javascript"]):
                        return False, "Response is not a video file", 0

                    content_length = int(resp.headers.get("Content-Length", 0))
                    if content_length > MAX_DOWNLOAD_SIZE:
                        return False, "File too large", 0

                    downloaded = 0
                    start_time = time.time()
                    last_update = 0.0
                    async with aiofiles.open(filepath, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            await f.write(chunk)
                            downloaded += len(chunk)
                            if downloaded > MAX_DOWNLOAD_SIZE:
                                _cleanup_file(filepath)
                                return False, "Download exceeded size limit", 0
                            now = time.time()
                            if now - last_update >= 2.0:
                                last_update = now
                                await progress_cb(
                                    _format_progress(
                                        downloaded, content_length, start_time, now
                                    )
                                )

                    if not _is_valid_video_content(filepath):
                        _cleanup_file(filepath)
                        return False, "Downloaded file is not a valid video", 0

                    return True, "", os.path.getsize(filepath)

        except asyncio.CancelledError:
            _cleanup_file(filepath)
            raise
        except Exception as e:
            error = str(e)[:150]

        if attempt < MAX_RETRIES:
            _cleanup_file(filepath)
            await asyncio.sleep(RETRY_DELAY * attempt)

    _cleanup_file(filepath)
    return False, error, 0


# ─── Download: Public API ──────────────────────────────────


async def download_hentaihaven_direct(
    url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود لینک مستقیم MP4."""
    if not _is_allowed_host(url):
        return False, "URL host not allowed", 0

    # yt-dlp اول (بهترین برای streaming servers)
    if shutil.which("yt-dlp"):
        logger.info("Trying download with yt-dlp: %s", url[:80])
        success, error, size = await _download_with_ytdlp(url, filepath, progress_cb)
        if success:
            return True, "", size
        logger.info("yt-dlp download failed: %s", error)
        _cleanup_file(filepath)

    # curl_cffi
    if _check_impersonation_support():
        logger.info("Trying download with curl_cffi: %s", url[:80])
        success, error, size = await _download_with_curl_cffi(url, filepath, progress_cb)
        if success:
            return True, "", size
        logger.info("curl_cffi download failed: %s", error)
        _cleanup_file(filepath)

    # aiohttp
    logger.info("Trying download with aiohttp: %s", url[:80])
    success, error, size = await _download_with_aiohttp(url, filepath, progress_cb)
    if success:
        return True, "", size

    _cleanup_file(filepath)
    return False, error, 0


async def download_hentaihaven_m3u8(
    m3u8_url: str, filepath: str, progress_cb: ProgressCallback,
) -> Tuple[bool, str, int]:
    """دانلود M3U8 stream با yt-dlp."""
    if not _is_allowed_host(m3u8_url):
        return False, "URL host not allowed", 0

    if not shutil.which("yt-dlp"):
        return False, "yt-dlp is not installed", 0

    success, error, size = await _download_with_ytdlp(
        m3u8_url, filepath, progress_cb, referer=_SITE_REFERER,
    )
    if success:
        return True, "", size
    _cleanup_file(filepath)
    return False, error, 0
