"""公开的系统信息 API（无需认证）"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import json

from app.database import get_db
from app.models import SystemSetting

router = APIRouter()

# 默认值
DEFAULT_SITE_NAME = "LMAICloud"
DEFAULT_SITE_DESCRIPTION = "大模型AI算力云平台"

SITE_INFO_KEYS = [
    "site_name", "site_description", "contact_email",
    "site_logo", "footer_text", "icp_number", "icp_link",
    "police_number", "copyright_text", "captcha_enabled",
]


@router.get("/site-info")
async def get_site_info(db: AsyncSession = Depends(get_db)):
    """获取站点公开信息（平台名称、描述、Logo、备案等）"""
    result = await db.execute(select(SystemSetting).where(
        SystemSetting.key.in_(SITE_INFO_KEYS)
    ))
    db_settings = {s.key: json.loads(s.value) for s in result.scalars().all()}
    
    return {
        "site_name": db_settings.get("site_name", DEFAULT_SITE_NAME),
        "site_description": db_settings.get("site_description", DEFAULT_SITE_DESCRIPTION),
        "contact_email": db_settings.get("contact_email", "support@lmaicloud.com"),
        "site_logo": db_settings.get("site_logo", ""),
        "footer_text": db_settings.get("footer_text", ""),
        "icp_number": db_settings.get("icp_number", ""),
        "icp_link": db_settings.get("icp_link", "https://beian.miit.gov.cn/"),
        "police_number": db_settings.get("police_number", ""),
        "copyright_text": db_settings.get("copyright_text", "© 2025 LMAICloud. All rights reserved."),
        "captcha_enabled": db_settings.get("captcha_enabled", True),
    }


@router.get("/agreements")
async def get_agreements(db: AsyncSession = Depends(get_db)):
    """获取用户协议/隐私政策等（公开）"""
    result = await db.execute(select(SystemSetting).where(
        SystemSetting.key.in_(["user_agreement", "privacy_policy", "service_agreement"])
    ))
    db_settings = {s.key: json.loads(s.value) for s in result.scalars().all()}
    
    return {
        "user_agreement": db_settings.get("user_agreement", ""),
        "privacy_policy": db_settings.get("privacy_policy", ""),
        "service_agreement": db_settings.get("service_agreement", ""),
    }
