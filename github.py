"""
github.py — آپلود فایل به GitHub repository و گرفتن لینک مستقیم دانلود
(نسخه ایمن با استفاده از متغیرهای محیطی)
"""

import asyncio
import base64
import logging
import os
import time
import re
from typing import Optional, Tuple

import aiohttp
import aiofiles
from aiohttp import ClientTimeout
from dotenv import load_dotenv   # <-- اضافه شد

# بارگذاری متغیرهای محیطی از فایل .env (در صورتی که وجود داشته باشد)
load_dotenv()

logger = logging.getLogger("GitHubUploader")

# ====================== CONFIGURATION ======================
# مقادیر از متغیرهای محیطی خوانده می‌شوند (در صورت نبود، پیش‌فرض None)
GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN")      # دیگر هاردکد نیست!
GITHUB_REPO     = os.getenv("GITHUB_REPO", "astwdnya/upanddown")
GITHUB_BRANCH   = os.getenv("GITHUB_BRANCH", "main")
GITHUB_BASE_DIR = os.getenv("GITHUB_BASE_DIR", "files")
GITHUB_MAX_MB   = int(os.getenv("GITHUB_MAX_MB", "50"))

# ====================== HELPERS ======================

def _raw_url(repo: str, branch: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"

def _api_url(repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{path}"

def _safe_name(filename: str) -> str:
    name = re.sub(r'[^\w.\-]', '_', filename)
    return name[:200] or f"file_{int(time.time())}"

def _subfolder_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in ['.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv']:
        return 'videos'
    elif ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']:
        return 'images'
    elif ext in ['.pdf']:
        return 'pdfs'
    elif ext in ['.mp3', '.aac', '.ogg', '.flac', '.wav']:
        return 'audio'
    else:
        return 'misc'

# ====================== MAIN UPLOAD FUNCTION ======================

async def upload_to_github(
    filepath: str,
    subfolder: Optional[str] = None,
    filename: Optional[str] = None,
    token: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> Tuple[bool, str, str]:
    """
    Returns: (success, message, download_url)
    """
    token    = token    or GITHUB_TOKEN
    repo     = repo     or GITHUB_REPO
    branch   = branch   or GITHUB_BRANCH
    base_dir = base_dir or GITHUB_BASE_DIR

    if not token:
        return False, "GITHUB_TOKEN is not set in environment variables.", ""
    if not repo:
        return False, "GITHUB_REPO is not set.", ""

    if not os.path.exists(filepath):
        return False, f"File not found: {filepath}", ""

    file_size = os.path.getsize(filepath)
    max_bytes = GITHUB_MAX_MB * 1024 * 1024
    if file_size > max_bytes:
        return False, f"File too large ({file_size/1024/1024:.1f}MB > {GITHUB_MAX_MB}MB)", ""

    orig_name = filename or os.path.basename(filepath)
    safe_name = _safe_name(orig_name)
    sub = subfolder or _subfolder_for(safe_name)

    name_noext, ext = os.path.splitext(safe_name)
    final_name = f"{name_noext}_{int(time.time())}{ext}"
    gh_path = f"{base_dir}/{sub}/{final_name}"

    async with aiofiles.open(filepath, 'rb') as f:
        raw = await f.read()
    content_b64 = base64.b64encode(raw).decode()

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitHub-Uploader-Bot/1.0",
        "Content-Type": "application/json",
    }

    payload = {
        "message": f"Upload {final_name} via bot",
        "content": content_b64,
        "branch": branch,
    }

    api_url = _api_url(repo, gh_path)
    timeout = ClientTimeout(total=120, connect=15)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        # بررسی وجود فایل قبلی (برای گرفتن sha در صورت نیاز)
        async with session.get(api_url) as check_resp:
            if check_resp.status == 200:
                existing = await check_resp.json()
                payload["sha"] = existing.get("sha", "")
            elif check_resp.status not in (200, 404):
                body = await check_resp.text()
                logger.error(f"GitHub check error {check_resp.status}: {body[:200]}")
                return False, f"GitHub check error: {check_resp.status}", ""

        # آپلود یا آپدیت فایل
        async with session.put(api_url, json=payload) as resp:
            if resp.status in (200, 201):
                raw_url = _raw_url(repo, branch, gh_path)
                logger.info(f"[GitHub] Uploaded: {gh_path} -> {raw_url}")
                return True, f"Uploaded to `{gh_path}`", raw_url
            else:
                body = await resp.text()
                logger.error(f"[GitHub] Upload failed: {resp.status} - {body[:300]}")
                return False, f"GitHub error {resp.status}: {body[:200]}", ""

def github_configured() -> bool:
    """بررسی می‌کند که توکن و مخزن تنظیم شده باشند"""
    return bool(GITHUB_TOKEN and GITHUB_REPO)
