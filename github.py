import asyncio
import base64
import logging
import os
import time
import re
import mimetypes
from typing import Optional, Tuple

import aiofiles
import aiohttp
from aiohttp import ClientTimeout
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("GitHubUploader")

# ====================== CONFIG ======================
GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN")
GITHUB_REPO     = os.getenv("GITHUB_REPO", "astwdnya/upanddown")
GITHUB_BRANCH   = os.getenv("GITHUB_BRANCH", "main")
GITHUB_BASE_DIR = os.getenv("GITHUB_BASE_DIR", "files")
GITHUB_MAX_MB   = int(os.getenv("GITHUB_MAX_MB", "100"))

# Contents API: فایل اصلی باید زیر ~74MB باشه
# (base64 حجم رو ۳۳٪ زیاد میکنه → 73MB × 1.33 ≈ 97MB < 100MB limit)
CONTENT_API_MAX_MB = 73

# Release tag ثابت برای همه آپلودها
RELEASE_TAG  = "uploads"
RELEASE_NAME = "File Uploads"

def github_configured() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_REPO)

def _raw_url(repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"

def _api_url(repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{path}"

def _safe_name(filename: str) -> str:
    name = re.sub(r'[^\w.\-() ]', '_', filename)
    return name[:200] or f"file_{int(time.time())}"

def _subfolder_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower().strip()
    video = {'.mp4','.mkv','.avi','.mov','.wmv','.flv','.webm','.mpeg','.mpg','.m4v','.3gp','.vob','.ts','.mts','.ogv','.rmvb','.asf','.f4v','.swf','.dv','.mxf','.avchd','.prores'}
    image = {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.tiff','.svg','.ico','.heic','.heif','.raw','.cr2','.nef','.arw','.dng','.psd','.ai','.eps'}
    doc   = {'.pdf','.xps','.epub','.mobi','.azw3','.cbr','.cbz','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.txt','.rtf','.csv','.json','.xml','.yaml','.html','.css','.js','.ts','.jsx','.tsx','.php','.py','.java','.cpp','.c','.cs','.go','.rs','.swift','.kt','.sh','.bat','.cmd','.ps1'}
    arch  = {'.zip','.rar','.7z','.tar','.gz','.bz2','.xz','.cab','.tgz','.jar','.war','.ear'}
    audio = {'.mp3','.wav','.aac','.flac','.ogg','.opus','.m4a','.wma','.amr','.midi','.ape','.alac','.caf'}
    app   = {'.apk','.xapk','.ipa','.deb','.dylib','.framework','.app','.pkg','.aab','.exe','.msi','.dll','.sys','.bin','.iso','.img','.dmg','.vhd','.vmdk','.qcow2','.nds','.rom','.cso','.nsp','.xci','.cia','.rvz','.wbfs','.pak','.obb','.unitypackage','.asset','.blend','.fbx','.obj','.stl','.gltf','.usdz'}
    cert  = {'.tor','.pem','.key','.cer','.p12','.mobileprovision','.keystore','.har','.pcap','.cap','.apkm','.apks','.dysm','.xcarchive','.xcodeproj'}
    if ext in video: return 'videos'
    if ext in image: return 'images'
    if ext in doc:   return 'documents'
    if ext in arch:  return 'archives'
    if ext in audio: return 'audio'
    if ext in app:   return 'apps'
    if ext in cert:  return 'certificates'
    return 'misc'

def _base_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "GitHub-Uploader-Bot",
    }

async def _update_progress(status_msg, percent: int, text: str):
    if not status_msg:
        return
    try:
        bar = '█' * (percent // 5) + '░' * (20 - percent // 5)
        await status_msg.edit(f"☁️ **{text}**\n`[{bar}]` **{percent}%**")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# روش ۱ — Contents API  (فایل اصلی <= 73 MB)
# ══════════════════════════════════════════════════════════════
async def _upload_contents_api(
    filepath: str,
    gh_path: str,
    final_name: str,
    status_msg,
) -> Tuple[bool, str, str]:

    await _update_progress(status_msg, 10, "Reading file...")
    async with aiofiles.open(filepath, 'rb') as f:
        raw = await f.read()

    await _update_progress(status_msg, 40, "Encoding...")
    content_b64 = base64.b64encode(raw).decode()

    payload = {
        "message": f"Upload {final_name}",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }
    api_url = _api_url(GITHUB_REPO, gh_path)

    # timeout: حداقل 3 دقیقه + هر MB یه ثانیه
    size_mb = len(raw) / (1024 * 1024)
    total_timeout = max(180, int(size_mb) + 120)
    timeout = ClientTimeout(total=total_timeout, connect=30, sock_read=total_timeout)

    await _update_progress(status_msg, 60, "Uploading to GitHub...")

    async with aiohttp.ClientSession(headers=_base_headers(), timeout=timeout) as session:
        # چک وجود فایل (برای SHA)
        async with session.get(api_url, timeout=ClientTimeout(total=15)) as check:
            if check.status == 200:
                data = await check.json()
                payload["sha"] = data.get("sha", "")

        async with session.put(api_url, json=payload) as resp:
            if resp.status in (200, 201):
                raw_url = _raw_url(GITHUB_REPO, GITHUB_BRANCH, gh_path)
                await _update_progress(status_msg, 100, "✅ Uploaded Successfully")
                logger.info(f"[GitHub/Contents] OK → {gh_path}")
                return True, "Success", raw_url
            else:
                body = await resp.text()
                logger.error(f"[GitHub/Contents] {resp.status}: {body[:300]}")
                await _update_progress(status_msg, 100, "❌ Failed")
                return False, f"Error {resp.status}: {body[:150]}", ""


# ══════════════════════════════════════════════════════════════
# روش ۲ — Releases API  (فایل بزرگتر از 73 MB، تا 2 GB)
# ══════════════════════════════════════════════════════════════
async def _ensure_release(session: aiohttp.ClientSession) -> Tuple[str, str]:
    """Release با tag مشخص رو پیدا یا میسازه. برمیگردونه: (release_id, upload_url)"""
    t = ClientTimeout(total=30)

    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{RELEASE_TAG}"
    async with session.get(url, timeout=t) as resp:
        if resp.status == 200:
            data = await resp.json()
            return str(data["id"]), data["upload_url"]

    create_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
    payload = {
        "tag_name": RELEASE_TAG,
        "name": RELEASE_NAME,
        "body": "Auto-uploaded via Telegram bot",
        "draft": False,
        "prerelease": False,
    }
    async with session.post(create_url, json=payload, timeout=t) as resp:
        if resp.status == 201:
            data = await resp.json()
            return str(data["id"]), data["upload_url"]
        body = await resp.text()
        raise RuntimeError(f"Could not create release: {resp.status} {body[:200]}")


async def _upload_release_asset(
    filepath: str,
    final_name: str,
    status_msg,
) -> Tuple[bool, str, str]:

    file_size = os.path.getsize(filepath)
    size_mb = file_size / (1024 * 1024)
    mime_type, _ = mimetypes.guess_type(filepath)
    mime_type = mime_type or "application/octet-stream"

    # timeout پویا: حداقل 5 دقیقه + هر MB 2 ثانیه
    total_timeout = max(300, int(size_mb * 2) + 180)
    timeout = ClientTimeout(total=total_timeout, connect=30, sock_read=total_timeout)

    await _update_progress(status_msg, 20, f"Preparing release upload ({size_mb:.0f} MB)...")

    async with aiohttp.ClientSession(headers=_base_headers(), timeout=timeout) as session:
        release_id, upload_url_tpl = await _ensure_release(session)
        upload_url = upload_url_tpl.split("{")[0] + f"?name={final_name}"

        await _update_progress(status_msg, 50, f"Uploading {size_mb:.0f} MB via Releases API...")
        logger.info(f"[GitHub/Release] {final_name} ({size_mb:.1f} MB) timeout={total_timeout}s")

        async with aiofiles.open(filepath, 'rb') as f:
            file_data = await f.read()

        upload_headers = {**_base_headers(), "Content-Type": mime_type}
        async with aiohttp.ClientSession(headers=upload_headers, timeout=timeout) as up_session:
            async with up_session.post(upload_url, data=file_data) as resp:
                body = await resp.json(content_type=None)
                if resp.status == 201:
                    dl_url = body.get("browser_download_url", "")
                    await _update_progress(status_msg, 100, "✅ Uploaded Successfully")
                    logger.info(f"[GitHub/Release] OK → {dl_url}")
                    return True, "Success", dl_url
                else:
                    msg = body.get("message", str(resp.status))
                    logger.error(f"[GitHub/Release] {resp.status}: {msg}")
                    await _update_progress(status_msg, 100, "❌ Failed")
                    return False, f"Error {resp.status}: {msg}", ""


# ══════════════════════════════════════════════════════════════
# تابع اصلی — bot.py فقط این رو صدا میزنه
# ══════════════════════════════════════════════════════════════
async def upload_to_github(
    filepath: str,
    status_msg=None,
    subfolder: Optional[str] = None,
    filename: Optional[str] = None,
) -> Tuple[bool, str, str]:

    if not github_configured():
        return False, "GitHub not configured (token or repo missing)", ""

    if not os.path.exists(filepath):
        return False, "File not found", ""

    file_size = os.path.getsize(filepath)
    size_mb = file_size / (1024 * 1024)

    if size_mb > GITHUB_MAX_MB:
        return False, f"File too large ({size_mb:.1f} MB > max {GITHUB_MAX_MB} MB)", ""

    orig_name  = filename or os.path.basename(filepath)
    safe_name  = _safe_name(orig_name)
    sub        = subfolder or _subfolder_for(safe_name)
    name_noext, ext = os.path.splitext(safe_name)
    final_name = f"{name_noext}_{int(time.time())}{ext}"
    gh_path    = f"{GITHUB_BASE_DIR}/{sub}/{final_name}"

    logger.info(f"[GitHub] {orig_name} → {gh_path} ({size_mb:.1f} MB)")

    try:
        if size_mb <= CONTENT_API_MAX_MB:
            logger.info(f"[GitHub] Using Contents API ({size_mb:.1f} MB ≤ {CONTENT_API_MAX_MB} MB)")
            return await _upload_contents_api(filepath, gh_path, final_name, status_msg)
        else:
            logger.info(f"[GitHub] Using Releases API ({size_mb:.1f} MB > {CONTENT_API_MAX_MB} MB)")
            return await _upload_release_asset(filepath, final_name, status_msg)

    except asyncio.TimeoutError:
        msg = f"Upload timed out ({size_mb:.1f} MB)"
        logger.error(f"[GitHub] {msg}")
        await _update_progress(status_msg, 100, f"❌ {msg}")
        return False, msg, ""
    except Exception as e:
        logger.error(f"[GitHub] Exception: {e}", exc_info=True)
        await _update_progress(status_msg, 100, "❌ Upload error")
        return False, str(e)[:200], ""
