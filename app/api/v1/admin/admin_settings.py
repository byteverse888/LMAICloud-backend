"""系统设置 API (管理端) - 数据库持久化存储"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr
from typing import Dict, Any, Optional
import json

from app.database import get_db
from app.models import SystemSetting
from app.utils.auth import get_current_admin_user
from app.services.email_service import send_test_email, get_email_config

router = APIRouter()

# 默认设置值
DEFAULT_SETTINGS: Dict[str, Any] = {
    "site_name": "LMAICloud",
    "site_description": "大模型AI算力云平台",
    "contact_email": "support@lmaicloud.com",
    "default_balance": 0.0,
    "min_recharge_amount": 10.0,
    "max_recharge_amount": 100000.0,
    "instance_auto_stop_hours": 24,
    "instance_max_per_user": 10,
    "storage_max_gb_per_user": 100,
    "price_adjustment_rate": 1.0,
    "maintenance_mode": False,
    "registration_enabled": True,
    "email_verification_required": True,
    "notification_email_enabled": True,
    # 邮件配置
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_from_email": "",
    "smtp_from_name": "LMAICloud",
    "smtp_use_tls": True,
    # 品牌配置
    "site_logo": "",
    "footer_text": "",
    "icp_number": "",
    "icp_link": "https://beian.miit.gov.cn/",
    "police_number": "",
    "copyright_text": "© 2025 LMAICloud. All rights reserved.",
    # 协议
    "user_agreement": "",
    "privacy_policy": "",
    "service_agreement": "",
    # 验证码
    "captcha_enabled": True,
}


class SystemSettingsUpdate(BaseModel):
    """系统设置更新"""
    site_name: Optional[str] = None
    site_description: Optional[str] = None
    contact_email: Optional[str] = None
    default_balance: Optional[float] = None
    min_recharge_amount: Optional[float] = None
    max_recharge_amount: Optional[float] = None
    instance_auto_stop_hours: Optional[int] = None
    instance_max_per_user: Optional[int] = None
    storage_max_gb_per_user: Optional[int] = None
    price_adjustment_rate: Optional[float] = None
    maintenance_mode: Optional[bool] = None
    registration_enabled: Optional[bool] = None
    email_verification_required: Optional[bool] = None
    notification_email_enabled: Optional[bool] = None
    # 品牌配置
    site_logo: Optional[str] = None
    footer_text: Optional[str] = None
    icp_number: Optional[str] = None
    icp_link: Optional[str] = None
    police_number: Optional[str] = None
    copyright_text: Optional[str] = None
    # 协议
    user_agreement: Optional[str] = None
    privacy_policy: Optional[str] = None
    service_agreement: Optional[str] = None
    # 验证码
    captcha_enabled: Optional[bool] = None


class EmailConfigUpdate(BaseModel):
    """邮件配置更新"""
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from_email: Optional[str] = None
    smtp_from_name: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    email_verification_required: Optional[bool] = None
    notification_email_enabled: Optional[bool] = None


class TestEmailRequest(BaseModel):
    """测试邮件请求"""
    email: EmailStr


async def get_all_settings(db: AsyncSession) -> Dict[str, Any]:
    """从数据库获取所有设置，不存在则返回默认值"""
    result = await db.execute(select(SystemSetting))
    db_settings = {s.key: json.loads(s.value) for s in result.scalars().all()}
    
    # 合并默认值和数据库值
    settings = DEFAULT_SETTINGS.copy()
    settings.update(db_settings)
    return settings


async def get_setting(db: AsyncSession, key: str, default: Any = None) -> Any:
    """获取单个设置值"""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        return json.loads(setting.value)
    return DEFAULT_SETTINGS.get(key, default)


async def set_setting(db: AsyncSession, key: str, value: Any) -> None:
    """设置单个配置项"""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    
    if setting:
        setting.value = json.dumps(value)
    else:
        setting = SystemSetting(key=key, value=json.dumps(value))
        db.add(setting)


@router.get("/")
async def get_settings(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取系统设置"""
    return await get_all_settings(db)


@router.put("/")
async def update_settings(
    settings: SystemSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """更新系统设置"""
    update_data = settings.model_dump(exclude_unset=True)
    
    for key, value in update_data.items():
        await set_setting(db, key, value)
    
    await db.commit()
    
    # 返回更新后的所有设置
    all_settings = await get_all_settings(db)
    return {"message": "设置已更新", "settings": all_settings}


@router.get("/pricing")
async def get_pricing_config(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取定价配置"""
    settings = await get_all_settings(db)
    return {
        "base_prices": {
            "RTX_4090": 3.5,
            "RTX_3090": 2.0,
            "A100_40G": 12.0,
            "A100_80G": 18.0,
            "H100_80G": 25.0,
            "V100_32G": 8.0,
        },
        "adjustment_rate": settings.get("price_adjustment_rate", 1.0),
        "discount_tiers": [
            {"hours": 100, "discount": 0.95},
            {"hours": 500, "discount": 0.90},
            {"hours": 1000, "discount": 0.85},
        ],
    }


@router.put("/pricing")
async def update_pricing_config(
    adjustment_rate: float,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """更新定价调整系数"""
    if adjustment_rate < 0.1 or adjustment_rate > 10:
        raise HTTPException(status_code=400, detail="调整系数必须在 0.1-10 之间")
    
    await set_setting(db, "price_adjustment_rate", adjustment_rate)
    await db.commit()
    return {"message": "定价调整系数已更新", "adjustment_rate": adjustment_rate}


@router.get("/email")
async def get_email_config_api(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取邮件配置"""
    settings = await get_all_settings(db)
    return {
        "smtp_host": settings.get("smtp_host", ""),
        "smtp_port": settings.get("smtp_port", 587),
        "smtp_user": settings.get("smtp_user", ""),
        "smtp_password": "******" if settings.get("smtp_password") else "",  # 密码不返回明文
        "smtp_from_email": settings.get("smtp_from_email", ""),
        "smtp_from_name": settings.get("smtp_from_name", "LMAICloud"),
        "smtp_use_tls": settings.get("smtp_use_tls", True),
        "notification_enabled": settings.get("notification_email_enabled", True),
        "verification_required": settings.get("email_verification_required", True),
        "is_configured": bool(settings.get("smtp_host") and settings.get("smtp_user") and settings.get("smtp_password")),
    }


@router.put("/email")
async def update_email_config_api(
    config: EmailConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """更新邮件配置"""
    update_data = config.model_dump(exclude_unset=True)
    
    for key, value in update_data.items():
        await set_setting(db, key, value)
    
    await db.commit()
    
    # 返回更新后的配置
    settings = await get_all_settings(db)
    return {
        "message": "邮件配置已更新",
        "config": {
            "smtp_host": settings.get("smtp_host", ""),
            "smtp_port": settings.get("smtp_port", 587),
            "smtp_user": settings.get("smtp_user", ""),
            "smtp_from_email": settings.get("smtp_from_email", ""),
            "smtp_from_name": settings.get("smtp_from_name", "LMAICloud"),
            "smtp_use_tls": settings.get("smtp_use_tls", True),
            "notification_enabled": settings.get("notification_email_enabled", True),
            "verification_required": settings.get("email_verification_required", True),
            "is_configured": bool(settings.get("smtp_host") and settings.get("smtp_user") and settings.get("smtp_password")),
        }
    }


@router.post("/email/test")
async def test_email_api(
    request: TestEmailRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """发送测试邮件"""
    # 检查邮件配置
    config = await get_email_config(db)
    if not config.is_configured:
        raise HTTPException(
            status_code=400,
            detail="邮件服务未配置，请先完成SMTP配置"
        )
    
    # 发送测试邮件
    success, error_msg = await send_test_email(db, request.email)
    
    if success:
        return {"message": f"测试邮件已发送到 {request.email}"}
    else:
        raise HTTPException(
            status_code=500,
            detail=error_msg or "邮件发送失败，请检查SMTP配置是否正确"
        )


@router.get("/maintenance")
async def get_maintenance_status(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取维护模式状态"""
    settings = await get_all_settings(db)
    return {
        "maintenance_mode": settings.get("maintenance_mode", False),
        "message": "系统维护中，请稍后访问",
    }


@router.put("/maintenance")
async def toggle_maintenance_mode(
    enabled: bool,
    message: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """切换维护模式"""
    await set_setting(db, "maintenance_mode", enabled)
    await db.commit()
    return {
        "message": f"维护模式已{'开启' if enabled else '关闭'}",
        "maintenance_mode": enabled,
    }


@router.get("/limits")
async def get_resource_limits(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取资源限制配置"""
    settings = await get_all_settings(db)
    return {
        "instance_max_per_user": settings.get("instance_max_per_user", 10),
        "storage_max_gb_per_user": settings.get("storage_max_gb_per_user", 100),
        "instance_auto_stop_hours": settings.get("instance_auto_stop_hours", 24),
    }


@router.put("/limits")
async def update_resource_limits(
    instance_max_per_user: Optional[int] = None,
    storage_max_gb_per_user: Optional[int] = None,
    instance_auto_stop_hours: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """更新资源限制配置"""
    if instance_max_per_user is not None:
        await set_setting(db, "instance_max_per_user", instance_max_per_user)
    if storage_max_gb_per_user is not None:
        await set_setting(db, "storage_max_gb_per_user", storage_max_gb_per_user)
    if instance_auto_stop_hours is not None:
        await set_setting(db, "instance_auto_stop_hours", instance_auto_stop_hours)
    
    await db.commit()
    settings = await get_all_settings(db)
    
    return {"message": "资源限制已更新", "limits": {
        "instance_max_per_user": settings.get("instance_max_per_user"),
        "storage_max_gb_per_user": settings.get("storage_max_gb_per_user"),
        "instance_auto_stop_hours": settings.get("instance_auto_stop_hours"),
    }}
