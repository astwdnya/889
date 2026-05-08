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
GITHUB_MAX_MB   = int(os.getenv("GITHUB_MAX_MB", "100"))   # حداکثر ۱۰۰ مگابایت برای Raw

def _raw_url(repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"

def _api_url(repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{path}"

def _safe_name(filename: str) -> str:
    name = re.sub(r'[^\w.\-() ]', '_', filename)
    return name[:200] or f"file_{int(time.time())}"

def _subfolder_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    
    # ==================== همه فرمت‌های درخواستی ====================
    video_ext = {'.mp4','.mkv','.avi','.mov','.wmv','.flv','.webm','.mpeg','.mpg','.m4v','.3gp','.vob',
                 '.ts','.mts','.ogv','.rmvb','.asf','.f4v','.swf','.dv','.mxf','.avchd','.prores'}
    
    image_ext = {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.tiff','.svg','.ico','.heic','.heif','.raw',
                 '.cr2','.nef','.arw','.dng','.psd','.ai','.eps'}
    
    document_ext = {'.pdf','.xps','.epub','.mobi','.azw3','.cbr','.cbz','.doc','.docx','.xls','.xlsx',
                    '.ppt','.pptx','.txt','.rtf','.csv','.json','.xml','.yaml','.html','.css','.js','.ts',
                    '.jsx','.tsx','.php','.py','.java','.cpp','.c','.cs','.go','.rs','.swift','.kt','.sh',
                    '.bat','.cmd','.ps1'}
    
    archive_ext = {'.zip','.rar','.7z','.tar','.gz','.bz2','.xz','.cab','.tgz','.jar','.war','.ear'}
    
    audio_ext = {'.mp3','.wav','.aac','.flac','.ogg','.opus','.m4a','.wma','.amr','.midi','.ape','.alac','.caf'}
    
    app_ext = {'.apk','.xapk','.ipa','.deb','.dylib','.framework','.app','.pkg','.aab','.exe','.msi','.dll',
               '.sys','.bin','.iso','.img','.dmg','.vhd','.vmdk','.qcow2','.nds','.rom','.cso','.nsp','.xci',
               '.cia','.rvz','.wbfs','.pak','.obb','.unitypackage','.asset','.blend','.fbx','.obj','.stl',
               '.gltf','.usdz'}
    
    cert_ext = {'.tor','.pem','.key','.cer','.p12','.mobileprovision','.keystore','.har','.pcap','.cap',
                '.apkm','.apks','.dysm','.xcarchive','.xcodeproj'}

    if ext in video_ext: return 'videos'
    if ext in image_ext: return 'images'
    if ext in document_ext: return 'documents'
    if ext in archive_ext: return 'archives'
    if ext in audio_ext: return 'audio'
    if ext in app_ext: return 'apps'
    if ext in cert_ext: return 'certificates'
    
    return 'misc'


async def upload_to_github(
    filepath: str,
    status_msg=None,           # برای نمایش پیشرفت
    subfolder: Optional[str] = None,
    filename: Optional[str] = None,
    token: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> Tuple[bool, str, str]:
    
    token = token or GITHUB_TOKEN
    repo = repo or GITHUB_REPO
    branch = branch or GITHUB_BRANCH
    base_dir = base_dir or GITHUB_BASE_DIR

    if not token or not repo:
        return False, "GitHub token or repo not configured", ""

    if not os.path.exists(filepath):
        return False, "File not found", ""

    file_size = os.path.getsize(filepath)
    if file_size > GITHUB_MAX_MB * 1024 * 1024:
        return False, f"File too large (max {GITHUB_MAX_MB}MB for Raw)", ""

    orig_name = filename or os.path.basename(filepath)
    safe_name = _safe_name(orig_name)
    sub = subfolder or _subfolder_for(safe_name)

    name_noext, ext = os.path.splitext(safe_name)
    final_name = f"{name_noext}_{int(time.time())}{ext}"
    gh_path = f"{base_dir}/{sub}/{final_name}"

    # ====================== PROGRESS BAR ======================
    async def update_progress(percent: int, status: str = "Uploading"):
        if status_msg:
            try:
                bar = '█' * (percent // 5) + '░' * (20 - percent // 5)
                await status_msg.edit(f"☁️ **{status} to GitHub**\n`[{bar}]` **{percent}%**")
            except:
                pass

    await update_progress(10, "Reading file")

    async with aiofiles.open(filepath, 'rb') as f:
        raw = await f.read()

    await update_progress(40, "Encoding...")

    content_b64 = base64.b64encode(raw).decode()

    await update_progress(60, "Connecting...")

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitHub-Uploader-Bot",
    }

    payload = {
        "message": f"Upload {final_name}",
        "content": content_b64,
        "branch": branch,
    }

    api_url = _api_url(repo, gh_path)

    async with aiohttp.ClientSession(headers=headers) as session:
        # چک وجود فایل
        async with session.get(api_url) as check:
            if check.status == 200:
                data = await check.json()
                payload["sha"] = data.get("sha")

        await update_progress(80, "Uploading to GitHub")

        async with session.put(api_url, json=payload) as resp:
            if resp.status in (200, 201):
                raw_url = _raw_url(repo, branch, gh_path)
                await update_progress(100, "✅ Uploaded Successfully")
                logger.info(f"[GitHub] Uploaded: {gh_path} → {raw_url}")
                return True, "Uploaded successfully", raw_url
            else:
                body = await resp.text()
                logger.error(f"GitHub Error {resp.status}: {body[:300]}")
                await update_progress(100, "❌ Upload Failed")
                return False, f"GitHub Error {resp.status}", ""

    return False, "Unknown error", ""
