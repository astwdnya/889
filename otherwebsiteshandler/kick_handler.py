"""
kick_handler.py
───────────────
دانلود و رکورد لایو استریم از Kick.com

روش کار (بر اساس تحلیل واقعی):

  ─── ساختار Kick.com ───
  1. API: https://kick.com/api/v2/channels/{channel}
     → playback_url: HLS master.m3u8 با JWT token (TTL کوتاه)
     → livestream.is_live: آیا لایو هست؟

  2. CDN: AWS IVS (playback.live-video.net)
     - master.m3u8: 5 کیفیت (160p, 360p, 480p, 720p, 1080p)
     - variant playlist: sliding window ~30s (7 segments)
     - segment URLs: امضا‌شده با JWT، قابل guess نیستن

  3. نکته مهم: DVR محدود
     - playlist type = VOD/LIVE (نه EVENT)
     - فقط sliding window فعلی در دسترسه (~30 ثانیه)
     - نمی‌تونیم از ابتدای لایو دانلود کنیم
     - ولی می‌تونیم از الان شروع به رکورد کنیم

  ─── Cloudflare ───
  - kick.com پشت Cloudflare هست
  - باید از curl_cffi با impersonate=chrome استفاده کنیم
  - playback.live-video.net (CDN) بدون CF هست

  ─── Token Expiry ───
  - JWT token در playback_url TTL کوتاه داره (~10 دقیقه)
  - باید هر چند دقیقه API رو دوباره صدا بزنیم تا token تازه بگیریم

سه مد عملیاتی:

  1. DOWNLOAD_PAST (دانلود گذشته)
     - دانلود همه segments موجود در sliding window فعلی
     - معمولاً ~30 ثانیه ویدیو
     - سریع، یه فایل خروجی

  2. RECORD_NOW (رکورد از الان)
     - شروع به دانلود segments جدید به‌صورت مداوم
     - هر 3 ثانیه playlist رو poll می‌کنه
     - segments جدید رو دانلود و به فایل اضافه می‌کنه
     - تا زمان Stop کاربر ادامه داره
     - وقتی فایل به 1.8GB رسید، پارت جدید می‌سازه

  3. HYBRID (دانلود گذشته + رکورد آینده) - موازی
     - یه task: دانلود sliding window فعلی
     - یه task همزمان: شروع به رکورد از الان
     - وقتی sliding window تموم شد، با رکورد ادغام می‌شه
     - پارت‌بندی 1.8GB

  ─── پارت‌بندی ───
  - وقتی فایل خروجی به 1.8GB رسید، بسته می‌شه
  - ffmpeg برای concat کردن TS segments و تبدیل به MP4
  - هر پارت به‌صورت جداگانه send می‌شه

  ─── Cancelation ───
  - کاربر می‌تونه هر زمان Stop کنه
  - پارت فعلی بسته می‌شه و ارسال می‌شه

وابستگی‌ها:
    pip install aiohttp aiofiles curl_cffi
    apt install ffmpeg
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin, unquote, parse_qs

import aiofiles
import aiohttp
from aiohttp import ClientTimeout, CookieJar

try:
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

logger = logging.getLogger("KickHandler")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ─── Constants ─────────────────────────────────────────────────────────────

MAX_PART_SIZE = 1.8 * 1024 * 1024 * 1024  # 1.8 GB
POLL_INTERVAL = 3.0  # هر 3 ثانیه playlist رو poll کن
TOKEN_REFRESH_INTERVAL = 300  # هر 5 دقیقه token رو refresh کن (JWT expiry ~10min)
SEGMENT_DOWNLOAD_TIMEOUT = 30
CONNECTOR_LIMIT = 50

# Quality preferences (height → variant index)
QUALITY_MAP = {
    "160p": 160,
    "360p": 360,
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
}

ProgressCallback = Callable[[str], Awaitable[None]]
PartReadyCallback = Callable[[str, int, int], Awaitable[None]]
# (filepath, part_num, size_bytes) → called when a part is ready to send


# ─── Utility ───────────────────────────────────────────────────────────────


def is_kick_url(url: str) -> bool:
    """بررسی اینکه URL مربوط به kick.com هست."""
    try:
        host = urlparse(url).hostname or ""
        return host in ("kick.com", "www.kick.com")
    except Exception:
        return False


def _extract_channel(url: str) -> str:
    """استخراج channel slug از URL.

    مثال‌ها:
      https://kick.com/clicker665 → clicker665
      https://kick.com/clicker665?tab=chat → clicker665
      https://kick.com/@clicker665 → clicker665
    """
    # Remove query string
    url = url.split("?")[0].split("#")[0]
    # Parse path
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return ""
    # Remove @ prefix
    if path.startswith("@"):
        path = path[1:]
    # Take first segment
    channel = path.split("/")[0]
    return channel.lower() if channel else ""


def _cleanup_file(filepath: str) -> None:
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        logger.warning("Failed to cleanup file %s: %s", filepath, e)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024 / 1024 / 1024:.2f} GB"


def _format_duration(seconds: float) -> str:
    """فرمت کردن مدت زمان به HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _check_curl_cffi() -> bool:
    return HAS_CURL_CFFI


# ─── API Client ───────────────────────────────────────────────────────────


class KickAPIClient:
    """کلاینت برای Kick.com API."""

    def __init__(self, channel: str):
        self.channel = channel
        self._playback_url = None
        self._playback_url_time = 0
        self._channel_data = None
        self._dvr_url = None
        self._live_url = None
        self._stream_uuid = None

    async def get_channel_data(self, force_refresh: bool = False) -> dict:
        """گرفتن اطلاعات کانال از API."""
        if self._channel_data and not force_refresh:
            # Cache for 30 seconds
            if time.time() - self._playback_url_time < 30:
                return self._channel_data

        if not _check_curl_cffi():
            raise RuntimeError("curl_cffi not installed (required for Kick.com Cloudflare bypass)")

        url = f"https://kick.com/api/v2/channels/{self.channel}"
        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"https://kick.com/{self.channel}",
        }

        async with AsyncSession() as s:
            resp = await s.get(url, impersonate="chrome", headers=headers, allow_redirects=True, timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(f"Kick API returned HTTP {resp.status_code}")
            self._channel_data = json.loads(resp.text)
            self._playback_url_time = time.time()
            return self._channel_data

    async def is_live(self) -> bool:
        """بررسی اینکه آیا کانال لایو هست."""
        data = await self.get_channel_data()
        ls = data.get("livestream")
        if not ls or not isinstance(ls, dict):
            return False
        return bool(ls.get("is_live"))

    async def _get_stream_uuid(self) -> str:
        """گرفتن stream UUID از channel page.

        نکته: UUID تو HTML صفحه channel هست و برای POST /api/v1/stream/{uuid}/playback
        لازمه تا DVR URL گرفته بشه.
        """
        if self._stream_uuid:
            return self._stream_uuid

        if not _check_curl_cffi():
            raise RuntimeError("curl_cffi not installed")

        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with AsyncSession() as s:
            resp = await s.get(f"https://kick.com/{self.channel}", impersonate="chrome", headers=headers, allow_redirects=True, timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(f"Channel page fetch failed: HTTP {resp.status_code}")

            # Search for UUID pattern (019f...)
            m = re.search(r'(019f[a-f0-9-]{28,36})', resp.text)
            if m:
                self._stream_uuid = m.group(1)
                logger.info(f"Found stream UUID: {self._stream_uuid}")
                return self._stream_uuid

            # Fallback: any UUID pattern
            m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', resp.text)
            if m:
                self._stream_uuid = m.group(1)
                logger.info(f"Found stream UUID (fallback): {self._stream_uuid}")
                return self._stream_uuid

        raise RuntimeError("Could not find stream UUID in channel page")

    async def _get_dvr_and_live_urls(self) -> Tuple[str, str]:
        """گرفتن DVR و Live URL از POST /api/v1/stream/{uuid}/playback.

        Returns:
            (dvr_url, live_url)
            dvr_url: URL برای دانلود از ابتدای لایو (EVENT playlist)
            live_url: URL برای دانلود sliding window فعلی
        """
        if self._dvr_url and self._live_url:
            return self._dvr_url, self._live_url

        uuid = await self._get_stream_uuid()

        if not _check_curl_cffi():
            raise RuntimeError("curl_cffi not installed")

        post_url = f"https://web.kick.com/api/v1/stream/{uuid}/playback"
        post_data = {
            "video_player": {
                "player": {
                    "player_name": "web",
                    "player_version": "web_a13f9698",
                    "player_software": "IVS Player",
                    "player_software_version": "1.53.0",
                },
                "mux_sdk": {"sdk_available": True},
                "pal_sdk": {"sdk_available": False, "nonce": ""},
                "datazoom_sdk": {"sdk_available": False},
                "google_ads_sdk": {"sdk_available": False},
            },
            "video_session": {
                "page_type": "channel",
                "player_remote_played": False,
                "enable_sampling": False,
                "url_path": self.channel,
                "autoplay_behaviour": "auto",
                "play_muted": False,
                "viewer_connection_type": "",
            },
            "user_session": {
                "session_id": "cb3925da-68e9-4776-9cf8-0882da40ac45",
                "player_device_id": "178d23f1-a735-458c-a547-2996895c5a27",
                "browser_lang": "en",
            },
        }

        headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://kick.com",
            "Referer": "https://kick.com/",
            "x-app-platform": "web",
        }

        async with AsyncSession() as s:
            resp = await s.post(post_url, impersonate="chrome", headers=headers, json=post_data, allow_redirects=True, timeout=15)
            if resp.status_code != 200:
                raise RuntimeError(f"Playback API returned HTTP {resp.status_code}")

            data = json.loads(resp.text)
            pb = data.get("playback_url", {})
            dvr_url = pb.get("dvr", "")
            live_url = pb.get("live", "")

            if not dvr_url and not live_url:
                raise RuntimeError("No DVR or live URL in playback response")

            self._dvr_url = dvr_url
            self._live_url = live_url
            logger.info(f"DVR URL: {dvr_url[:100]}..." if dvr_url else "No DVR URL")
            logger.info(f"Live URL: {live_url[:100]}..." if live_url else "No live URL")

            return dvr_url, live_url

    async def get_playback_url(self, force_refresh: bool = False) -> str:
        """گرفتن live playback URL (HLS master.m3u8)."""
        if force_refresh or time.time() - self._playback_url_time > TOKEN_REFRESH_INTERVAL:
            await self.get_channel_data(force_refresh=force_refresh)

        if not self._channel_data:
            raise RuntimeError("No channel data available")

        pb = self._channel_data.get("playback_url")
        if not pb:
            raise RuntimeError("No playback_url in channel data")

        self._playback_url = pb
        return pb

    async def get_dvr_url(self) -> str:
        """گرفتن DVR URL برای دانلود از ابتدای لایو."""
        dvr, _ = await self._get_dvr_and_live_urls()
        return dvr

    async def get_live_url(self) -> str:
        """گرفتن live URL برای دانلود sliding window."""
        _, live = await self._get_dvr_and_live_urls()
        return live

    async def get_stream_info(self) -> dict:
        """گرفتن اطلاعات لایو (duration, title, etc.)."""
        data = await self.get_channel_data()
        ls = data.get("livestream", {})
        return {
            "is_live": bool(ls.get("is_live")) if ls else False,
            "title": ls.get("session_title", "") if ls else "",
            "created_at": ls.get("created_at", "") if ls else "",
            "duration": ls.get("duration", 0) if ls else 0,
            "viewer_count": ls.get("viewer_count", 0) if ls else 0,
            "thumbnail": ls.get("thumbnail", "") if ls else "",
        }


# ─── HLS Parser ───────────────────────────────────────────────────────────


class HLSPlaylist:
    """Parser برای HLS playlist."""

    def __init__(self, content: str, base_url: str = ""):
        self.content = content
        self.base_url = base_url
        self.media_sequence = 0
        self.target_duration = 6
        self.playlist_type = ""
        self.segments: List[Dict[str, Any]] = []
        self.elapsed_secs = 0
        self.total_secs = 0
        self._parse()

    def _parse(self):
        lines = self.content.strip().split("\n")
        current_extinf = 0
        current_pdt = ""
        current_byterange = None
        current_base_url = None

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                self.media_sequence = int(line.split(":")[1])
            elif line.startswith("#EXT-X-TARGETDURATION:"):
                self.target_duration = int(float(line.split(":")[1]))
            elif line.startswith("#EXT-X-PLAYLIST-TYPE:"):
                self.playlist_type = line.split(":")[1].strip()
            elif line.startswith("#EXT-X-NET-LIVE-VIDEO-ELAPSED-SECS:"):
                self.elapsed_secs = float(line.split(":")[1])
            elif line.startswith("#EXT-X-NET-LIVE-VIDEO-TOTAL-SECS:"):
                self.total_secs = float(line.split(":")[1])
            elif line.startswith("#EXTINF:"):
                parts = line[8:].split(",")
                current_extinf = float(parts[0]) if parts else 0
            elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                current_pdt = line.split(":", 1)[1].strip()
            elif line.startswith("#EXT-X-BYTERANGE:"):
                # Parse byte range: e.g. "167508@0" means length 167508 starting at offset 0
                br = line.split(":")[1].strip()
                if "@" in br:
                    length, offset = br.split("@")
                    current_byterange = (int(length), int(offset))
                else:
                    current_byterange = (int(br), None)
            elif line.startswith("#"):
                continue
            else:
                # This is a segment URI
                seg_url = line
                if seg_url.startswith("/"):
                    parsed = urlparse(self.base_url)
                    seg_url = f"{parsed.scheme}://{parsed.netloc}{seg_url}"
                elif not seg_url.startswith("http"):
                    seg_url = urljoin(self.base_url, seg_url)

                seg_entry = {
                    "url": seg_url,
                    "duration": current_extinf,
                    "program_date_time": current_pdt,
                    "sequence": self.media_sequence + len(self.segments),
                    "byte_range": None,
                }

                # Handle byte-range
                if current_byterange:
                    length, offset = current_byterange
                    if offset is None:
                        # Use previous offset + length
                        offset = seg_entry.get("_last_offset", 0)
                    seg_entry["byte_range"] = f"{offset}-{offset + length - 1}"
                    seg_entry["_last_offset"] = offset + length

                self.segments.append(seg_entry)
                current_extinf = 0
                current_pdt = ""
                # Note: byte_range persists for subsequent segments if not re-specified
                # But we reset it here — it will be re-set by next #EXT-X-BYTERANGE if needed
                # Actually for byte-range playlists, the offset auto-increments
                # We need to track this properly
                if current_byterange and current_byterange[1] is not None:
                    # Auto-increment for next segment if no explicit offset
                    current_byterange = (current_byterange[0], None)


async def fetch_master_playlist(playback_url: str, referer: str = "https://kick.com/") -> str:
    """Fetch master.m3u8 content."""
    if not _check_curl_cffi():
        raise RuntimeError("curl_cffi not installed")

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }

    async with AsyncSession() as s:
        resp = await s.get(playback_url, impersonate="chrome", headers=headers, allow_redirects=True, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"master.m3u8 fetch failed: HTTP {resp.status_code}")
        return resp.text


async def fetch_variant_playlist(variant_url: str, referer: str = "https://kick.com/") -> str:
    """Fetch variant playlist content."""
    if not _check_curl_cffi():
        raise RuntimeError("curl_cffi not installed")

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }

    async with AsyncSession() as s:
        resp = await s.get(variant_url, impersonate="chrome", headers=headers, allow_redirects=True, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError(f"variant playlist fetch failed: HTTP {resp.status_code}")
        return resp.text


def parse_master_playlist(content: str, base_url: str) -> List[dict]:
    """Parse master.m3u8 و استخراج variant playlists."""
    variants = []
    lines = content.strip().split("\n")
    # base_dir = base_url without the filename (for resolving relative variant URLs)
    base_dir = base_url.rsplit("/", 1)[0] + "/"

    for i, line in enumerate(lines):
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            if i + 1 < len(lines):
                variant_url = lines[i + 1].strip()
                attrs = {}
                attr_str = line[len("#EXT-X-STREAM-INF:"):]
                for m in re.finditer(r'(\w+)=(?:"([^"]+)"|([^,]+))', attr_str):
                    k = m.group(1)
                    v = m.group(2) or m.group(3)
                    attrs[k] = v

                # Resolve URL (relative to base_dir, not base_url)
                if variant_url.startswith("/"):
                    parsed = urlparse(base_url)
                    variant_url = f"{parsed.scheme}://{parsed.netloc}{variant_url}"
                elif not variant_url.startswith("http"):
                    variant_url = urljoin(base_dir, variant_url)

                resolution = attrs.get("RESOLUTION", "")
                try:
                    height = int(resolution.split("x")[1]) if "x" in resolution else 0
                except (ValueError, IndexError):
                    height = 0

                variants.append({
                    "url": variant_url,
                    "resolution": resolution,
                    "height": height,
                    "bandwidth": int(attrs.get("BANDWIDTH", 0)),
                    "codecs": attrs.get("CODECS", ""),
                    "frame_rate": attrs.get("FRAME-RATE", ""),
                })

    # Sort by height descending
    variants.sort(key=lambda v: v["height"], reverse=True)
    return variants


def select_variant(variants: List[dict], quality: str = "720p") -> dict:
    """انتخاب variant با توجه به کیفیت درخواستی.

    quality: "1080p", "720p", "480p", "360p", "160p", "best", "worst"
    """
    if not variants:
        raise RuntimeError("No variants available")

    if quality == "best":
        return variants[0]
    if quality == "worst":
        return variants[-1]

    target_height = QUALITY_MAP.get(quality, 720)

    # Find exact match
    for v in variants:
        if v["height"] == target_height:
            return v

    # Find closest lower
    best = variants[-1]
    for v in variants:
        if v["height"] <= target_height and v["height"] >= best["height"]:
            best = v
    return best


async def download_segment(url: str, filepath: str, referer: str = "https://kick.com/", byte_range: Optional[str] = None) -> bool:
    """دانلود یه segment.

    Args:
        url: segment URL
        filepath: مسیر ذخیره
        referer: Referer header
        byte_range: اگه segment از byte-range استفاده کنه (DVR), این رو set کن
                    مثلاً "0-167507"
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
    }
    if byte_range:
        headers["Range"] = f"bytes={byte_range}"

    try:
        async with AsyncSession() as s:
            resp = await s.get(url, impersonate="chrome", headers=headers, allow_redirects=True, timeout=SEGMENT_DOWNLOAD_TIMEOUT)
            if resp.status_code not in (200, 206):
                logger.warning(f"Segment download failed: HTTP {resp.status_code}")
                return False
            data = resp.content if hasattr(resp, "content") else resp.body
            async with aiofiles.open(filepath, "wb") as f:
                await f.write(data)
            return True
    except Exception as e:
        logger.warning(f"Segment download error: {e}")
        return False


# ─── HLS Recorder ─────────────────────────────────────────────────────────


class HLSRecorder:
    """رکوردر برای لایو استریم Kick.com."""

    def __init__(
        self,
        channel: str,
        quality: str = "720p",
        output_dir: str = "/tmp/kick_recordings",
        part_ready_cb: Optional[PartReadyCallback] = None,
        progress_cb: Optional[ProgressCallback] = None,
    ):
        self.channel = channel
        self.quality = quality
        self.output_dir = output_dir
        self.part_ready_cb = part_ready_cb
        self.progress_cb = progress_cb

        self.api_client = KickAPIClient(channel)
        self.recording = False
        self.cancelled = False
        self.paused = False

        self._playback_url = None
        self._playback_url_time = 0
        self._master_variants = None
        self._master_url = None
        self._variant_url = None
        self._downloaded_segments: set = set()
        self._current_part_num = 0
        self._current_part_size = 0
        self._current_part_path = None
        self._current_ts_list = []  # list of TS file paths for current part
        self._start_time = 0
        self._total_bytes = 0
        self._total_segments = 0

        os.makedirs(output_dir, exist_ok=True)

    async def _notify(self, msg: str):
        if self.progress_cb:
            try:
                await self.progress_cb(msg)
            except Exception:
                pass

    async def _notify_part_ready(self, filepath: str, part_num: int, size: int):
        if self.part_ready_cb:
            try:
                await self.part_ready_cb(filepath, part_num, size)
            except Exception as e:
                logger.warning(f"part_ready_cb error: {e}")

    async def _refresh_playback_url(self):
        """Refresh playback URL از API (JWT token expiry)."""
        self._playback_url = await self.api_client.get_playback_url(force_refresh=True)
        self._playback_url_time = time.time()
        logger.info(f"Refreshed playback URL")

    async def _get_variant_url(self, use_dvr: bool = False) -> str:
        """گرفتن variant URL برای کیفیت انتخاب شده.

        Args:
            use_dvr: اگه True، از DVR URL استفاده کن (دانلود از ابتدای لایو).
                     اگه False، از live URL استفاده کن (sliding window).
        """
        # Refresh if needed
        if use_dvr:
            master_url = await self.api_client.get_dvr_url()
        else:
            if time.time() - self._playback_url_time > TOKEN_REFRESH_INTERVAL:
                await self._refresh_playback_url()
            master_url = self._playback_url or await self.api_client.get_playback_url()

        # Fetch master if needed (or if URL changed)
        if not self._master_variants or self._master_url != master_url:
            master_content = await fetch_master_playlist(master_url)
            self._master_variants = parse_master_playlist(master_content, master_url)
            self._master_url = master_url
            logger.info(f"Found {len(self._master_variants)} variants for {'DVR' if use_dvr else 'live'}")

        # Select variant
        variant = select_variant(self._master_variants, self.quality)
        self._variant_url = variant["url"]
        logger.info(f"Selected variant: {variant['resolution']} ({variant['bandwidth']} bps)")
        return self._variant_url

    def _start_new_part(self):
        """شروع یه پارت جدید."""
        self._current_part_num += 1
        self._current_part_size = 0
        self._current_ts_list = []
        # Create temp dir for this part's segments
        part_dir = os.path.join(self.output_dir, f"part_{self._current_part_num:04d}")
        os.makedirs(part_dir, exist_ok=True)
        self._current_part_path = part_dir

    async def _finalize_part(self) -> Optional[str]:
        """بستن پارت فعلی و تبدیل به MP4.

        Returns:
            path to finalized MP4 file, or None if no segments
        """
        if not self._current_ts_list or self._current_part_num == 0:
            return None

        part_num = self._current_part_num
        ts_dir = self._current_part_path
        mp4_path = os.path.join(self.output_dir, f"{self.channel}_part_{part_num:04d}.mp4")

        # Build filelist for ffmpeg
        list_path = os.path.join(ts_dir, "filelist.txt")
        async with aiofiles.open(list_path, "w") as f:
            for ts_file in self._current_ts_list:
                # ffmpeg concat needs absolute paths with single quotes
                abs_path = os.path.abspath(ts_file)
                await f.write(f"file '{abs_path}'\n")

        # Run ffmpeg to concat and convert to MP4
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            "-movflags", "+faststart",
            mp4_path,
        ]

        logger.info(f"[Part {part_num}] Finalizing with ffmpeg...")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.wait()

        if process.returncode != 0:
            stderr = (await process.stderr.read()).decode(errors="replace")
            logger.error(f"[Part {part_num}] ffmpeg failed: {stderr[:300]}")
            return None

        # Verify output
        if not os.path.exists(mp4_path):
            logger.error(f"[Part {part_num}] MP4 not created")
            return None

        mp4_size = os.path.getsize(mp4_path)
        logger.info(f"[Part {part_num}] Finalized: {mp4_path} ({_format_size(mp4_size)})")

        # Clean up TS files
        for ts_file in self._current_ts_list:
            try:
                os.remove(ts_file)
            except OSError:
                pass
        try:
            os.remove(list_path)
            os.rmdir(ts_dir)
        except OSError:
            pass

        # Notify
        await self._notify_part_ready(mp4_path, part_num, mp4_size)

        return mp4_path

    async def _download_segment_to_part(self, seg: dict) -> int:
        """دانلود segment و اضافه کردن به پارت فعلی.

        Args:
            seg: segment dict with url, sequence, byte_range

        Returns:
            size of downloaded segment in bytes
        """
        if not self._current_part_path:
            self._start_new_part()

        seg_seq = seg["sequence"]
        byte_range = seg.get("byte_range")
        ts_path = os.path.join(self._current_part_path, f"seg_{seg_seq:08d}.ts")
        success = await download_segment(seg["url"], ts_path, byte_range=byte_range)
        if not success:
            return 0

        size = os.path.getsize(ts_path)
        self._current_ts_list.append(ts_path)
        self._current_part_size += size
        self._total_bytes += size
        self._total_segments += 1
        self._downloaded_segments.add(seg_seq)

        return size

    async def _check_part_overflow(self):
        """اگه پارت فعلی به 1.8GB رسید، بسته بشه و پارت جدید شروع بشه."""
        if self._current_part_size >= MAX_PART_SIZE:
            await self._notify(f"📦 Part {self._current_part_num} reached {_format_size(self._current_part_size)} — finalizing...")
            await self._finalize_part()
            self._start_new_part()

    async def _record_loop(self, download_existing: bool = False):
        """حلقه اصلی رکورد.

        Args:
            download_existing: اگه True، اول segments موجود در sliding window رو دانلود کن.
        """
        await self._notify("🎬 Starting recording...")

        self._start_time = time.time()
        self.recording = True

        # Get variant URL (use live URL for recording, not DVR)
        try:
            variant_url = await self._get_variant_url(use_dvr=False)
        except Exception as e:
            await self._notify(f"❌ Error getting variant URL: {e}")
            self.recording = False
            return

        # Start first part
        self._start_new_part()

        last_notify = 0
        last_token_refresh = time.time()

        while self.recording and not self.cancelled:
            if self.paused:
                await asyncio.sleep(1)
                continue

            # Check token refresh
            if time.time() - last_token_refresh > TOKEN_REFRESH_INTERVAL:
                try:
                    await self._refresh_playback_url()
                    # Re-fetch variant URL (might change)
                    variant_url = await self._get_variant_url()
                    last_token_refresh = time.time()
                except Exception as e:
                    logger.warning(f"Token refresh failed: {e}")

            try:
                # Fetch variant playlist
                playlist_content = await fetch_variant_playlist(variant_url)
                playlist = HLSPlaylist(playlist_content, variant_url)

                # Download new segments
                new_count = 0
                for seg in playlist.segments:
                    seg_seq = seg["sequence"]
                    if seg_seq in self._downloaded_segments:
                        continue

                    if self.cancelled:
                        break

                    size = await self._download_segment_to_part(seg)
                    if size > 0:
                        new_count += 1
                        await self._check_part_overflow()

                # Notify progress
                now = time.time()
                if now - last_notify > 5 or new_count > 0:
                    elapsed = now - self._start_time
                    await self._notify(
                        f"🔴 Recording {self.channel} ({self.quality})\n"
                        f"⏱ {_format_duration(elapsed)} elapsed\n"
                        f"📦 Part {self._current_part_num}: {_format_size(self._current_part_size)}/{_format_size(MAX_PART_SIZE)}\n"
                        f"📊 Total: {_format_size(self._total_bytes)}, {self._total_segments} segments"
                    )
                    last_notify = now

            except Exception as e:
                logger.warning(f"Record loop error: {e}")
                await self._notify(f"⚠ Error: {e}")

            # Wait before next poll
            await asyncio.sleep(POLL_INTERVAL)

        # Finalize last part
        if self._current_ts_list:
            await self._notify(f"📦 Finalizing part {self._current_part_num}...")
            await self._finalize_part()

        self.recording = False
        await self._notify(f"✅ Recording stopped. Total: {_format_size(self._total_bytes)}, {self._total_segments} segments")

    # ─── Public API ───

    async def record_from_now(self):
        """شروع رکورد از الان به بعد."""
        await self._record_loop(download_existing=False)

    async def download_past(self):
        """دانلود کل لایو از ابتدا تا الان (DVR).

        نکته: این متد از DVR URL استفاده می‌کنه که یه EVENT playlist با همه segments
        از ابتدای لایو داره. ممکنه هزاران segment باشه و طول بکشه.
        """
        await self._notify("📥 Downloading past stream from beginning (DVR)...")

        self._start_time = time.time()

        try:
            # Use DVR URL for past download
            variant_url = await self._get_variant_url(use_dvr=True)
            playlist_content = await fetch_variant_playlist(variant_url)
            playlist = HLSPlaylist(playlist_content, variant_url)

            total_segs = len(playlist.segments)
            await self._notify(f"📊 Found {total_segs} segments ({_format_duration(total_segs * playlist.target_duration)} approx)")

            self._start_new_part()

            for i, seg in enumerate(playlist.segments):
                if self.cancelled:
                    break

                size = await self._download_segment_to_part(seg)
                if size > 0:
                    await self._check_part_overflow()

                # Progress every 50 segments
                if (i + 1) % 50 == 0:
                    elapsed = time.time() - self._start_time
                    await self._notify(
                        f"📥 Downloading past... {i+1}/{total_segs} segments\n"
                        f"📦 Part {self._current_part_num}: {_format_size(self._current_part_size)}\n"
                        f"⏱ {_format_duration(elapsed)} elapsed"
                    )

            # Finalize
            if self._current_ts_list:
                await self._notify(f"📦 Finalizing part {self._current_part_num}...")
                await self._finalize_part()

            elapsed = time.time() - self._start_time
            await self._notify(
                f"✅ Downloaded {self._total_segments} segments ({_format_size(self._total_bytes)}) "
                f"in {_format_duration(elapsed)}"
            )

        except Exception as e:
            await self._notify(f"❌ Error: {e}")
            logger.error(f"download_past error: {e}", exc_info=True)

    async def hybrid_record(self):
        """دانلود گذشته + رکورد آینده به‌صورت موازی."""
        await self._notify("🎬 Starting hybrid mode (past + record)...")

        # Just run the record loop — it'll download existing segments first
        await self._record_loop(download_existing=True)

    def stop(self):
        """توقف رکورد."""
        self.cancelled = True
        self.recording = False
        logger.info("Recording stop requested")

    def pause(self):
        """pause رکورد."""
        self.paused = True

    def resume(self):
        """از سرگیری رکورد."""
        self.paused = False


# ─── High-level API ───────────────────────────────────────────────────────


async def get_channel_info(url: str) -> dict:
    """گرفتن اطلاعات کانال.

    Returns:
        {is_live, title, channel, stream_info}
    """
    channel = _extract_channel(url)
    if not channel:
        return {"error": "Invalid Kick URL — could not extract channel"}

    client = KickAPIClient(channel)
    try:
        is_live = await client.is_live()
        stream_info = await client.get_stream_info()
        return {
            "channel": channel,
            "is_live": is_live,
            "title": stream_info["title"],
            "duration": stream_info["duration"],
            "viewer_count": stream_info["viewer_count"],
            "thumbnail": stream_info["thumbnail"],
            "stream_info": stream_info,
        }
    except Exception as e:
        return {"error": str(e), "channel": channel}


async def get_available_qualities(url: str) -> List[dict]:
    """گرفتن لیست کیفیت‌های موجود.

    Returns:
        list of {height, resolution, bandwidth}
    """
    channel = _extract_channel(url)
    if not channel:
        return []

    client = KickAPIClient(channel)
    try:
        # Try DVR URL first (has all qualities)
        try:
            dvr_url = await client.get_dvr_url()
            if dvr_url:
                master_content = await fetch_master_playlist(dvr_url)
                variants = parse_master_playlist(master_content, dvr_url)
                if variants:
                    return [
                        {
                            "height": v["height"],
                            "resolution": v["resolution"],
                            "bandwidth": v["bandwidth"],
                            "quality_key": f"{v['height']}p",
                        }
                        for v in variants
                    ]
        except Exception as e:
            logger.debug(f"DVR qualities failed: {e}")

        # Fallback to live URL
        pb_url = await client.get_playback_url()
        master_content = await fetch_master_playlist(pb_url)
        variants = parse_master_playlist(master_content, pb_url)
        return [
            {
                "height": v["height"],
                "resolution": v["resolution"],
                "bandwidth": v["bandwidth"],
                "quality_key": f"{v['height']}p",
            }
            for v in variants
        ]
    except Exception as e:
        logger.error(f"get_available_qualities error: {e}")
        return []


async def download_past(
    url: str,
    output_dir: str,
    quality: str = "720p",
    progress_cb: Optional[ProgressCallback] = None,
    part_ready_cb: Optional[PartReadyCallback] = None,
) -> dict:
    """دانلود sliding window فعلی.

    Returns:
        {success, parts: [filepath], total_size, error}
    """
    channel = _extract_channel(url)
    if not channel:
        return {"success": False, "error": "Invalid Kick URL"}

    recorder = HLSRecorder(
        channel=channel,
        quality=quality,
        output_dir=output_dir,
        part_ready_cb=part_ready_cb,
        progress_cb=progress_cb,
    )

    parts = []

    async def wrapped_part_cb(filepath, part_num, size):
        parts.append({"filepath": filepath, "part_num": part_num, "size": size})
        if part_ready_cb:
            await part_ready_cb(filepath, part_num, size)

    recorder.part_ready_cb = wrapped_part_cb

    await recorder.download_past()

    return {
        "success": len(parts) > 0,
        "parts": parts,
        "total_size": recorder._total_bytes,
        "total_segments": recorder._total_segments,
    }


async def record_from_now(
    url: str,
    output_dir: str,
    quality: str = "720p",
    progress_cb: Optional[ProgressCallback] = None,
    part_ready_cb: Optional[PartReadyCallback] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> dict:
    """رکورد از الان به بعد.

    Args:
        stop_event: وقتی set بشه، رکورد متوقف می‌شه.

    Returns:
        {success, parts: [filepath], total_size, total_segments, error}
    """
    channel = _extract_channel(url)
    if not channel:
        return {"success": False, "error": "Invalid Kick URL"}

    recorder = HLSRecorder(
        channel=channel,
        quality=quality,
        output_dir=output_dir,
        part_ready_cb=part_ready_cb,
        progress_cb=progress_cb,
    )

    parts = []

    async def wrapped_part_cb(filepath, part_num, size):
        parts.append({"filepath": filepath, "part_num": part_num, "size": size})
        if part_ready_cb:
            await part_ready_cb(filepath, part_num, size)

    recorder.part_ready_cb = wrapped_part_cb

    # Start recording in background
    record_task = asyncio.create_task(recorder.record_from_now())

    # Wait for stop event
    if stop_event:
        await stop_event.wait()
        recorder.stop()

    # Wait for recording to finish
    await record_task

    return {
        "success": len(parts) > 0,
        "parts": parts,
        "total_size": recorder._total_bytes,
        "total_segments": recorder._total_segments,
    }


async def hybrid_record(
    url: str,
    output_dir: str,
    quality: str = "720p",
    progress_cb: Optional[ProgressCallback] = None,
    part_ready_cb: Optional[PartReadyCallback] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> dict:
    """دانلود گذشته + رکورد آینده به‌صورت موازی.

    Args:
        stop_event: وقتی set بشه، رکورد متوقف می‌شه.

    Returns:
        {success, parts: [filepath], total_size, total_segments, error}
    """
    channel = _extract_channel(url)
    if not channel:
        return {"success": False, "error": "Invalid Kick URL"}

    recorder = HLSRecorder(
        channel=channel,
        quality=quality,
        output_dir=output_dir,
        part_ready_cb=part_ready_cb,
        progress_cb=progress_cb,
    )

    parts = []

    async def wrapped_part_cb(filepath, part_num, size):
        parts.append({"filepath": filepath, "part_num": part_num, "size": size})
        if part_ready_cb:
            await part_ready_cb(filepath, part_num, size)

    recorder.part_ready_cb = wrapped_part_cb

    # Start recording in background
    record_task = asyncio.create_task(recorder.hybrid_record())

    # Wait for stop event
    if stop_event:
        await stop_event.wait()
        recorder.stop()

    # Wait for recording to finish
    await record_task

    return {
        "success": len(parts) > 0,
        "parts": parts,
        "total_size": recorder._total_bytes,
        "total_segments": recorder._total_segments,
    }


# ─── Self-test ────────────────────────────────────────────────────────────


async def _self_test():
    """تست خودي هندلر."""
    test_url = "https://kick.com/buddha"
    print(f"\n{'═' * 80}")
    print(f"Self-test: {test_url}")
    print(f"{'═' * 80}\n")

    async def progress(msg):
        print(f"  → {msg}")

    async def part_ready(filepath, part_num, size):
        print(f"  📦 Part {part_num} ready: {filepath} ({_format_size(size)})")

    # 1. Get channel info
    print("\n1. Getting channel info...")
    info = await get_channel_info(test_url)
    print(f"  Channel: {info.get('channel')}")
    print(f"  Is live: {info.get('is_live')}")
    print(f"  Title: {info.get('title', '')[:80]}")
    print(f"  Duration: {info.get('duration')}s")
    print(f"  Viewers: {info.get('viewer_count')}")

    if info.get("error"):
        print(f"  Error: {info['error']}")
        return

    if not info.get("is_live"):
        print("\n  ⚠ Channel is not live — cannot test recording")
        return

    # 2. Get available qualities
    print("\n2. Getting available qualities...")
    qualities = await get_available_qualities(test_url)
    for q in qualities:
        print(f"  {q['quality_key']}: {q['resolution']} ({q['bandwidth']} bps)")

    # 3. Download past (sliding window)
    print("\n3. Downloading past (sliding window)...")
    output_dir = "/home/z/my-project/logs/kick_test"
    os.makedirs(output_dir, exist_ok=True)

    result = await download_past(
        test_url, output_dir, quality="720p",
        progress_cb=progress, part_ready_cb=part_ready,
    )
    print(f"\n  Result: {result}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(_self_test())
