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

# ========== 协议默认内容 ==========
_DEFAULT_USER_AGREEMENT = (
    '<h2>用户协议</h2>'
    '<p>欢迎使用本平台（以下简称"平台"）。请您在使用前仔细阅读本协议。注册或使用本平台即表示您同意以下条款。</p>'
    '<h3>一、服务说明</h3>'
    '<p>本平台为用户提供大模型AI算力云服务，包括但不限于GPU实例创建、镜像管理、模型训练与推理等功能。平台有权根据业务需要调整服务内容并提前通知用户。</p>'
    '<h3>二、账户注册与安全</h3>'
    '<p>1. 用户须使用真实有效的邮箱注册账户，并妥善保管账户密码。<br/>2. 因用户自身原因导致的账户泄露或被盗，平台不承担责任。<br/>3. 用户不得将账户转让、借用或出售给第三方。</p>'
    '<h3>三、使用规范</h3>'
    '<p>用户在使用平台时须遵守以下规范：<br/>1. 不得利用平台从事违法违规活动；<br/>2. 不得干扰平台正常运行或攻击平台系统；<br/>3. 不得上传或传播含有恶意代码的文件；<br/>4. 不得利用平台资源进行加密货币挖矿；<br/>5. 合理使用平台资源，不得恶意占用。</p>'
    '<h3>四、知识产权</h3>'
    '<p>平台上的所有内容（包括但不限于文字、图片、代码、界面设计）的知识产权归平台所有。用户在平台上创建的模型和数据，其知识产权归用户所有。</p>'
    '<h3>五、免责声明</h3>'
    '<p>1. 因不可抗力导致的服务中断，平台不承担责任；<br/>2. 用户因自身操作不当导致的数据丢失，平台不承担责任；<br/>3. 平台不对用户使用服务产生的结果承担担保责任。</p>'
    '<h3>六、协议变更</h3>'
    '<p>平台有权根据需要修改本协议，修改后的协议将在平台公示。用户继续使用平台即视为同意修改后的协议。</p>'
)

_DEFAULT_PRIVACY_POLICY = (
    '<h2>隐私政策</h2>'
    '<p>本平台重视用户隐私保护。本政策说明我们如何收集、使用和保护您的个人信息。</p>'
    '<h3>一、信息收集</h3>'
    '<p>我们可能收集以下信息：<br/>1. 注册信息：邮箱地址、用户名等；<br/>2. 使用信息：登录记录、操作日志、实例使用情况；<br/>3. 支付信息：充值记录、交易流水（不存储银行卡号等敏感信息）；<br/>4. 设备信息：浏览器类型、IP地址等。</p>'
    '<h3>二、信息使用</h3>'
    '<p>收集的信息用于：<br/>1. 提供和改善平台服务；<br/>2. 账户管理和身份验证；<br/>3. 发送服务通知和系统公告；<br/>4. 安全风控和防止欺诈；<br/>5. 数据分析和服务优化。</p>'
    '<h3>三、信息保护</h3>'
    '<p>1. 我们采用行业标准的安全措施保护用户数据；<br/>2. 用户密码经过加密存储，任何人无法获取明文；<br/>3. 严格限制员工访问用户数据的权限；<br/>4. 定期进行安全审计和漏洞扫描。</p>'
    '<h3>四、Cookie 使用</h3>'
    '<p>本平台使用Cookie和类似技术来维持用户会话和改善使用体验。您可以通过浏览器设置管理Cookie偏好。</p>'
    '<h3>五、信息共享</h3>'
    '<p>我们不会向第三方出售用户个人信息。仅在以下情况可能共享：<br/>1. 获得用户明确同意；<br/>2. 法律法规要求；<br/>3. 与关联公司共享以提供服务（受同等保护措施约束）。</p>'
    '<h3>六、用户权利</h3>'
    '<p>您有权：<br/>1. 查询和更正您的个人信息；<br/>2. 删除您的账户和相关数据；<br/>3. 撤回授权同意；<br/>4. 对个人信息处理提出异议。<br/>如需行使上述权利，请联系客服邮箱。</p>'
)

_DEFAULT_SERVICE_AGREEMENT = (
    '<h2>产品服务协议</h2>'
    '<p>本协议规定了平台向用户提供产品和服务的具体条款。</p>'
    '<h3>一、服务范围</h3>'
    '<p>本平台提供以下服务：<br/>1. GPU云实例：按需创建和管理GPU计算实例；<br/>2. 镜像服务：提供预置和自定义镜像管理；<br/>3. 存储服务：提供数据存储和管理功能；<br/>4. 应用市场：提供预置AI应用和模型部署。</p>'
    '<h3>二、计费规则</h3>'
    '<p>1. 按量计费：根据实际使用时长和资源规格计费，最小计费单位为1小时；<br/>2. 套餐计费：用户可购买月卡等套餐享受优惠；<br/>3. 余额充值：支持微信支付等方式充值，充值后不可提现；<br/>4. 欠费处理：账户余额不足时，运行中的实例将被自动停止。</p>'
    '<h3>三、退款政策</h3>'
    '<p>1. 账户余额不支持退款提现；<br/>2. 因平台原因导致的服务不可用，将按实际影响时长进行补偿；<br/>3. 套餐类产品一经购买不支持退款。</p>'
    '<h3>四、服务保障（SLA）</h3>'
    '<p>1. 平台承诺月度服务可用性不低于99.5%；<br/>2. 计划内维护将提前24小时通知用户；<br/>3. 非计划停机将在发现后第一时间通知用户并尽快恢复；<br/>4. 因SLA未达标造成的损失，将根据影响程度给予账户余额补偿。</p>'
    '<h3>五、数据安全</h3>'
    '<p>1. 用户数据存储在安全的基础设施上；<br/>2. 平台不主动访问用户实例内的数据；<br/>3. 实例释放后，相关数据将在7天内彻底删除；<br/>4. 建议用户定期备份重要数据。</p>'
    '<h3>六、违约责任</h3>'
    '<p>1. 用户违反使用规范，平台有权暂停或终止服务；<br/>2. 因用户违规导致平台损失的，用户应承担赔偿责任；<br/>3. 因平台原因导致用户损失的，赔偿上限为用户最近12个月支付的费用总额。</p>'
)

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
    "user_agreement": _DEFAULT_USER_AGREEMENT,
    "privacy_policy": _DEFAULT_PRIVACY_POLICY,
    "service_agreement": _DEFAULT_SERVICE_AGREEMENT,
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
    for key, value in db_settings.items():
        settings[key] = value
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
