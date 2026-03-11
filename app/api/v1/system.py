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


@router.get("/site-info")
async def get_site_info(db: AsyncSession = Depends(get_db)):
    """获取站点公开信息（平台名称、描述等）"""
    result = await db.execute(select(SystemSetting).where(
        SystemSetting.key.in_(["site_name", "site_description", "contact_email"])
    ))
    db_settings = {s.key: json.loads(s.value) for s in result.scalars().all()}
    
    return {
        "site_name": db_settings.get("site_name", DEFAULT_SITE_NAME),
        "site_description": db_settings.get("site_description", DEFAULT_SITE_DESCRIPTION),
        "contact_email": db_settings.get("contact_email", "support@lmaicloud.com"),
    }
