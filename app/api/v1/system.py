"""公开的系统信息 API（无需认证）"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import json
from pathlib import Path

from app.database import get_db
from app.models import SystemSetting
from app.config import settings as app_settings

router = APIRouter()

# 默认值从 config 统一获取
DEFAULT_SITE_DESCRIPTION = "大模型AI算力云平台"

SITE_INFO_KEYS = [
    "site_name", "site_description", "contact_email",
    "site_logo", "footer_text", "icp_number", "icp_link",
    "police_number", "copyright_text", "captcha_enabled",
    "announcement_text",
]


@router.get("/site-info")
async def get_site_info(db: AsyncSession = Depends(get_db)):
    """获取站点公开信息（平台名称、描述、Logo、备案等）"""
    result = await db.execute(select(SystemSetting).where(
        SystemSetting.key.in_(SITE_INFO_KEYS)
    ))
    db_settings = {s.key: json.loads(s.value) for s in result.scalars().all()}
    
    return {
        "site_name": db_settings.get("site_name", app_settings.app_name),
        "site_description": db_settings.get("site_description", DEFAULT_SITE_DESCRIPTION),
        "contact_email": db_settings.get("contact_email", "support@lmaicloud.com"),
        "site_logo": db_settings.get("site_logo", ""),
        "footer_text": db_settings.get("footer_text", ""),
        "icp_number": db_settings.get("icp_number", ""),
        "icp_link": db_settings.get("icp_link", "https://beian.miit.gov.cn/"),
        "police_number": db_settings.get("police_number", ""),
        "copyright_text": db_settings.get("copyright_text", ""),
        "captcha_enabled": db_settings.get("captcha_enabled", True),
        "announcement_text": db_settings.get("announcement_text", ""),
    }


@router.get("/logo/{filename}")
async def get_logo(filename: str):
    """提供 Logo 静态文件访问（无需认证）"""
    # 安全校验：防止路径遍历
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="无效的文件名")
    
    logo_dir = Path(app_settings.storage_root) / "_system" / "logos"
    filepath = logo_dir / filename
    
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Logo 不存在")
    
    # 根据后缀判断 MIME 类型
    suffix = filepath.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
    }
    media_type = media_types.get(suffix, "application/octet-stream")
    
    return FileResponse(
        filepath,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )

