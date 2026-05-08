# github.py
import asyncio
import base64
import logging
import os
import time
import re
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

def github_configured() -> bool:
    """بررسی اینکه آیا توکن و ریپو تنظیم شده‌اند"""
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
    if ext in doc: return 'documents'
    if ext in arch: return 'archives'
    if ext in audio: return 'audio'
    if ext in app: return 'apps'
    if ext in cert: return 'certificates'
    return 'misc'


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
    if file_size > GITHUB_MAX_MB * 1024 * 1024:
        return False, f"File too large (max {GITHUB_MAX_MB}MB)", ""

    orig_name = filename or os.path.basename(filepath)
    safe_name = _safe_name(orig_name)
    sub = subfolder or _subfolder_for(safe_name)

    name_noext, ext = os.path.splitext(safe_name)
    final_name = f"{name_noext}_{int(time.time())}{ext}"
    gh_path = f"{GITHUB_BASE_DIR}/{sub}/{final_name}"

    async def update_progress(percent: int, text: str):
        if status_msg:
            try:
                bar = '█' * (percent // 5) + '░' * (20 - percent // 5)
                await status_msg.edit(f"☁️ **{text}**\n`[{bar}]` **{percent}%**")
            except:
                pass

    await update_progress(10, "Reading file...")

    async with aiofiles.open(filepath, 'rb') as f:
        raw = await f.read()

    await update_progress(40, "Encoding...")

    content_b64 = base64.b64encode(raw).decode()

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitHub-Uploader-Bot",
    }

    payload = {
        "message": f"Upload {final_name}",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }

    api_url = _api_url(GITHUB_REPO, gh_path)

    await update_progress(70, "Uploading to GitHub...")

    async with aiohttp.ClientSession(headers=headers) as session:
        # چک وجود فایل
        async with session.get(api_url) as check:
            if check.status == 200:
                data = await check.json()
                payload["sha"] = data.get("sha")

        async with session.put(api_url, json=payload) as resp:
            if resp.status in (200, 201):
                raw_url = _raw_url(GITHUB_REPO, GITHUB_BRANCH, gh_path)
                await update_progress(100, "✅ Uploaded Successfully")
                logger.info(f"[GitHub] Uploaded: {gh_path}")
                return True, "Success", raw_url
            else:
                body = await resp.text()
                logger.error(f"GitHub Error {resp.status}: {body[:200]}")
                await update_progress(100, "❌ Failed")
                return False, f"Error {resp.status}", ""

    return False, "Unknown error", ""
