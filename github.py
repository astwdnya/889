"""
github.py — آپلود فایل به GitHub از طریق Release Assets (حداکثر ۲GB)
"""
import logging
import os
import time
import re
from typing import Optional, Tuple
import aiohttp
import aiofiles
from aiohttp import ClientTimeout
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("GitHubUploader")

# ====================== CONFIGURATION ======================
GITHUB_TOKEN       = os.getenv("GITHUB_TOKEN")
GITHUB_REPO        = os.getenv("GITHUB_REPO", "astwdnya/upanddown")
GITHUB_BRANCH      = os.getenv("GITHUB_BRANCH", "main")
GITHUB_BASE_DIR    = os.getenv("GITHUB_BASE_DIR", "files")
GITHUB_MAX_MB      = int(os.getenv("GITHUB_MAX_MB", "2000"))          # ← 2GB
GITHUB_RELEASE_TAG = os.getenv("GITHUB_RELEASE_TAG", "bot-uploads")   # tag ثابت

# ====================== HELPERS ======================
def _safe_name(filename: str) -> str:
    name = re.sub(r'[^\w.\-]', '_', filename)
    return name[:200] or f"file_{int(time.time())}"

def _api_base(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}"

# ====================== RELEASE MANAGEMENT ======================
async def _get_or_create_release(session, repo, tag, branch):
    """Release با tag مشخص رو پیدا یا بساز. Returns: (release_id, upload_url_base)"""
    base = _api_base(repo)

    async with session.get(f"{base}/releases/tags/{tag}") as resp:
        if resp.status == 200:
            data = await resp.json()
            upload_url = data["upload_url"].split("{")[0]
            logger.info(f"[GitHub] Found existing release: {tag} (id={data['id']})")
            return data["id"], upload_url

    payload = {
        "tag_name": tag,
        "target_commitish": branch,
        "name": "Bot Uploads",
        "body": "Auto-generated release for bot file uploads.",
        "draft": False,
        "prerelease": False,
    }
    async with session.post(f"{base}/releases", json=payload) as resp:
        if resp.status == 201:
            data = await resp.json()
            upload_url = data["upload_url"].split("{")[0]
            logger.info(f"[GitHub] Created new release: {tag} (id={data['id']})")
            return data["id"], upload_url
        body = await resp.text()
        logger.error(f"[GitHub] Create release failed: {resp.status} - {body[:300]}")
        return None, None

async def _delete_existing_asset(session, repo, release_id, filename):
    """اگه asset با همین نام وجود داره حذفش کن."""
    base = _api_base(repo)
    async with session.get(f"{base}/releases/{release_id}/assets") as resp:
        if resp.status != 200:
            return
        assets = await resp.json()
    for asset in assets:
        if asset["name"] == filename:
            async with session.delete(f"{base}/releases/assets/{asset['id']}") as del_resp:
                if del_resp.status == 204:
                    logger.info(f"[GitHub] Deleted old asset: {filename}")
            break

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
    """Returns: (success, message, download_url)"""
    token  = token  or GITHUB_TOKEN
    repo   = repo   or GITHUB_REPO
    branch = branch or GITHUB_BRANCH

    if not token:
        return False, "GITHUB_TOKEN is not set.", ""
    if not repo:
        return False, "GITHUB_REPO is not set.", ""
    if not os.path.exists(filepath):
        return False, f"File not found: {filepath}", ""

    file_size = os.path.getsize(filepath)
    max_bytes = GITHUB_MAX_MB * 1024 * 1024
    if file_size > max_bytes:
        return False, f"File too large ({file_size/1024/1024:.1f}MB > {GITHUB_MAX_MB}MB)", ""

    orig_name  = filename or os.path.basename(filepath)
    safe_name  = _safe_name(orig_name)
    name_noext, ext = os.path.splitext(safe_name)
    final_name = f"{name_noext}_{int(time.time())}{ext}"

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "GitHub-Uploader-Bot/2.0",
    }
    timeout = ClientTimeout(total=3600, connect=30, sock_read=300)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        release_id, upload_url_base = await _get_or_create_release(
            session, repo, GITHUB_RELEASE_TAG, branch
        )
        if not release_id or not upload_url_base:
            return False, "Could not get or create GitHub release.", ""

        await _delete_existing_asset(session, repo, release_id, final_name)

        upload_url = f"{upload_url_base}?name={final_name}"
        upload_headers = {
            **headers,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(file_size),
        }

        logger.info(f"[GitHub] Uploading {final_name} ({file_size/1024/1024:.1f}MB)...")

        async with aiofiles.open(filepath, "rb") as f:
            file_data = await f.read()

        async with session.post(upload_url, data=file_data, headers=upload_headers) as resp:
            if resp.status == 201:
                data = await resp.json()
                download_url = data["browser_download_url"]
                logger.info(f"[GitHub] Done: {download_url}")
                return True, f"Uploaded as `{final_name}`", download_url
            body = await resp.text()
            logger.error(f"[GitHub] Upload failed: {resp.status} - {body[:300]}")
            return False, f"GitHub error {resp.status}: {body[:200]}", ""


def github_configured() -> bool:
    return bool(GITHUB_TOKEN and GITHUB_REPO)
