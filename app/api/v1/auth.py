from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr
from typing import Optional
import re
import random
import string
import secrets
from datetime import datetime, timedelta
from collections import defaultdict

from app.database import get_db
from app.models import User, SystemSetting
from app.schemas import UserCreate, UserLogin, UserResponse, LoginResponse
from app.utils.auth import get_password_hash, verify_password, create_access_token, create_refresh_token, decode_token, get_current_user
from app.logging_config import get_logger
from app.config import settings
from app.services.email_service import send_activation_email, send_password_reset_email, get_email_config
from app.api.v1.points import add_points
from app.models import PointType
import json

router = APIRouter()
logger = get_logger("lmaicloud.auth")

# ── 验证码存储 ────────────────────────────────────────────────
# 优先使用 Redis，降级使用内存 dict
_redis_available: Optional[bool] = None
_redis_client = None


async def _get_redis():
    """惰性获取 Redis 连接"""
    global _redis_available, _redis_client
    if _redis_available is False:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await _redis_client.ping()
        _redis_available = True
        logger.info("验证码存储: 使用 Redis")
        return _redis_client
    except Exception:
        _redis_available = False
        logger.warning("验证码存储: Redis 不可用，使用内存模式")
        return None


# 内存降级存储
verify_codes: dict[str, dict] = {}
captcha_store: dict[str, dict] = {}


async def _set_captcha(captcha_id: str, answer: str, ttl: int = 300):
    """存储验证码（优先Redis，降级内存）"""
    r = await _get_redis()
    if r:
        await r.setex(f"captcha:{captcha_id}", ttl, answer)
    else:
        captcha_store[captcha_id] = {
            'answer': answer,
            'expires_at': datetime.now() + timedelta(seconds=ttl),
        }


async def _get_and_delete_captcha(captcha_id: str) -> Optional[str]:
    """获取并删除验证码"""
    r = await _get_redis()
    if r:
        answer = await r.getdel(f"captcha:{captcha_id}")
        return answer
    else:
        stored = captcha_store.pop(captcha_id, None)
        if stored and datetime.now() < stored['expires_at']:
            return stored['answer']
        return None

# ── 登录速率限制 ──────────────────────────────────────────────
# {ip_or_email: [timestamp, ...]}  —— 滑动窗口
_login_attempts: dict[str, list[float]] = defaultdict(list)
LOGIN_RATE_WINDOW = 900     # 15分钟窗口
LOGIN_MAX_ATTEMPTS_IP = 30  # 同一IP 15分钟内最多30次
LOGIN_MAX_ATTEMPTS_EMAIL = 10  # 同一邮箱 15分钟内最多10次


def _check_login_rate(key: str, max_attempts: int):
    """检查滑动窗口速率限制"""
    now = datetime.now().timestamp()
    attempts = _login_attempts[key]
    # 清除窗口外的记录
    _login_attempts[key] = [t for t in attempts if now - t < LOGIN_RATE_WINDOW]
    if len(_login_attempts[key]) >= max_attempts:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="登录尝试次数过多，请15分钟后再试"
        )
    _login_attempts[key].append(now)


# ── 密码强度验证 ─────────────────────────────────────────────
def validate_password_strength(password: str):
    """
    密码策略：8-64位，至少包含大写、小写、数字中的两种
    """
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码长度至少8位")
    if len(password) > 64:
        raise HTTPException(status_code=400, detail="密码长度不能超过64位")
    checks = [
        bool(re.search(r'[A-Z]', password)),
        bool(re.search(r'[a-z]', password)),
        bool(re.search(r'[0-9]', password)),
        bool(re.search(r'[^A-Za-z0-9]', password)),
    ]
    if sum(checks) < 2:
        raise HTTPException(
            status_code=400,
            detail="密码需包含大写字母、小写字母、数字、特殊字符中的至少两种"
        )


class SendCodeRequest(BaseModel):
    email: EmailStr


class CodeLoginRequest(BaseModel):
    email: EmailStr
    code: str


class ForgotPasswordRequest(BaseModel):
    """忘记密码请求"""
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """重置密码请求"""
    token: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    """修改密码请求"""
    old_password: str
    new_password: str


class ActivateEmailRequest(BaseModel):
    """邮箱激活请求"""
    token: str


class ResendActivationRequest(BaseModel):
    """重新发送激活邮件请求"""
    email: EmailStr


class RefreshTokenRequest(BaseModel):
    """刷新Token请求"""
    refresh_token: str


class CaptchaLoginRequest(BaseModel):
    """带验证码的登录请求"""
    captcha_id: Optional[str] = None
    captcha_code: Optional[str] = None


def generate_code(length: int = 6) -> str:
    """生成随机验证码"""
    return ''.join(random.choices(string.digits, k=length))


def generate_activation_token() -> str:
    """生成激活令牌"""
    return secrets.token_urlsafe(32)


async def get_setting_value(db: AsyncSession, key: str, default=None):
    """获取系统设置值"""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        return json.loads(setting.value)
    return default


async def send_email(email: str, code: str):
    """发送邮件（模拟）"""
    # TODO: 集成真实邮件服务（如 SendGrid, AWS SES）
    logger.info(f"发送验证码到 {email}")


@router.post("/send-code")
async def send_verify_code(
    request: SendCodeRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """发送邮箱验证码"""
    email = request.email
    logger.info(f"请求发送验证码: {email}")
    
    # 检查是否频繁发送
    if email in verify_codes:
        last_sent = verify_codes[email].get('sent_at')
        if last_sent and datetime.now() - last_sent < timedelta(seconds=60):
            logger.warning(f"验证码请求过于频繁: {email}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="请求过于频繁，请稍后再试"
            )
    
    # 生成验证码
    code = generate_code()
    verify_codes[email] = {
        'code': code,
        'sent_at': datetime.now(),
        'expires_at': datetime.now() + timedelta(minutes=10)
    }
    
    # 后台发送邮件
    background_tasks.add_task(send_email, email, code)
    
    return {"message": "验证码已发送", "expires_in": 600}


@router.post("/login-with-code", response_model=LoginResponse)
async def login_with_code(
    request: CodeLoginRequest,
    db: AsyncSession = Depends(get_db)
):
    """验证码登录"""
    email = request.email
    code = request.code
    
    # 验证验证码
    stored = verify_codes.get(email)
    if not stored:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="验证码不存在或已过期"
        )
    
    if datetime.now() > stored['expires_at']:
        del verify_codes[email]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="验证码已过期"
        )
    
    if stored['code'] != code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="验证码错误"
        )
    
    # 验证成功，删除验证码
    del verify_codes[email]
    
    # 查找或创建用户
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    
    if not user:
        # 自动注册新用户
        user = User(
            email=email,
            password_hash=get_password_hash(generate_code(12)),  # 随机密码
            nickname=email.split("@")[0]
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    
    access_token = create_access_token(data={"sub": str(user.id)})
    
    return LoginResponse(user=user, token=access_token)


@router.post("/register")
async def register(
    user_data: UserCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """用户注册 - 发送激活邮件"""
    logger.info(f"用户注册请求: {user_data.email}")

    # 密码强度校验
    validate_password_strength(user_data.password)
    
    # Check if user exists
    result = await db.execute(select(User).where(User.email == user_data.email))
    existing_user = result.scalar_one_or_none()
    
    if existing_user:
        # 如果用户已存在但未激活，可以重新发送激活邮件
        if not existing_user.verified:
            logger.info(f"用户已存在但未激活，重新发送激活邮件: {user_data.email}")
            # 生成新的激活令牌
            activation_token = generate_activation_token()
            expire_hours = settings.email_activation_expire_hours
            existing_user.activation_token = activation_token
            existing_user.activation_expires_at = datetime.utcnow() + timedelta(hours=expire_hours)
            # 更新密码（用户可能想要修改密码）
            existing_user.password_hash = get_password_hash(user_data.password)
            await db.commit()
            
            # 获取站点名称
            site_name = await get_setting_value(db, "site_name", settings.app_name)
            
            # 后台发送激活邮件
            background_tasks.add_task(
                send_activation_email,
                db,
                user_data.email,
                activation_token,
                site_name,
                expire_hours
            )
            
            return {
                "message": "激活邮件已发送，请查收邮箱",
                "email": user_data.email,
                "need_activation": True
            }
        
        logger.warning(f"注册失败-邮箱已存在: {user_data.email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="邮箱已被注册"
        )
    
    # 检查是否需要邮箱验证
    email_verification_required = await get_setting_value(db, "email_verification_required", True)
    
    # 生成激活令牌
    activation_token = generate_activation_token()
    expire_hours = settings.email_activation_expire_hours
    
    # Create new user
    hashed_password = get_password_hash(user_data.password)
    user = User(
        email=user_data.email,
        password_hash=hashed_password,
        nickname=user_data.nickname or user_data.email.split("@")[0],
        verified=not email_verification_required,  # 如果不需要验证，直接设为已验证
        activation_token=activation_token if email_verification_required else None,
        activation_expires_at=datetime.utcnow() + timedelta(hours=expire_hours) if email_verification_required else None
    )
    
    # 处理邀请码
    invite_code_value = getattr(user_data, 'invite_code', None)
    inviter = None
    if invite_code_value:
        inviter_result = await db.execute(
            select(User).where(User.invite_code == invite_code_value)
        )
        inviter = inviter_result.scalar_one_or_none()
        if inviter:
            user.invited_by = inviter.id
    
    db.add(user)
    await db.commit()
    await db.refresh(user)
    
    # 绑定邀请关系（积分奖励延迟到激活时发放）
    if inviter:
        pass  # invited_by 已在上方设置，奖励在 activate_email 中发放
    
    logger.info(f"用户注册成功: {user.email}, ID: {user.id}")

    # 记录注册日志
    try:
        from app.api.v1.audit_log import create_audit_log, get_client_ip
        from app.models import AuditAction, AuditResourceType
        client_ip = get_client_ip(request)
        await create_audit_log(
            db, user.id, AuditAction.REGISTER, AuditResourceType.ACCOUNT,
            resource_name=user.email,
            detail=f"新用户注册",
            ip_address=client_ip,
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"记录注册日志失败: {e}")

    # 如果需要邮箱验证，发送激活邮件
    if email_verification_required:
        # 获取站点名称
        site_name = await get_setting_value(db, "site_name", settings.app_name)
        
        # 后台发送激活邮件
        background_tasks.add_task(
            send_activation_email,
            db,
            user.email,
            activation_token,
            site_name,
            expire_hours
        )
        
        return {
            "message": "注册成功！激活邮件已发送，请查收邮箱",
            "email": user.email,
            "need_activation": True
        }
    
    return {
        "message": "注册成功！",
        "email": user.email,
        "need_activation": False
    }


@router.post("/activate")
async def activate_email(
    request: ActivateEmailRequest,
    db: AsyncSession = Depends(get_db)
):
    """激活邮箱"""
    logger.info(f"邮箱激活请求: token={request.token[:10]}...")
    
    # 查找用户
    result = await db.execute(
        select(User).where(User.activation_token == request.token)
    )
    user = result.scalar_one_or_none()
    
    if not user:
        logger.warning(f"激活失败-无效的激活令牌")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无效的激活链接"
        )
    
    # 检查是否已激活
    if user.verified:
        return {"message": "邮箱已激活，可以登录", "already_activated": True}
    
    # 检查令牌是否过期
    if user.activation_expires_at and datetime.utcnow() > user.activation_expires_at:
        logger.warning(f"激活失败-令牌已过期: {user.email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="激活链接已过期，请重新注册或申请发送激活邮件"
        )
    
    # 激活用户
    user.verified = True
    user.activation_token = None
    user.activation_expires_at = None
    await db.commit()

    # 如果是被邀请用户，激活后给邀请人发放积分奖励
    if user.invited_by:
        try:
            await add_points(db, user.invited_by, 50, PointType.INVITE_REWARD, f"邀请用户 {user.email} 激活奖励")
            await db.commit()
            logger.info(f"邀请奖励已发放: inviter={user.invited_by}, invited={user.email}")
        except Exception as e:
            logger.warning(f"发放邀请奖励失败: {e}")

    logger.info(f"邮箱激活成功: {user.email}")
    return {"message": "邮箱激活成功！现在可以登录了", "activated": True}


@router.post("/resend-activation")
async def resend_activation_email(
    request: ResendActivationRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """重新发送激活邮件"""
    logger.info(f"重新发送激活邮件请求: {request.email}")
    
    # 查找用户
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalar_one_or_none()
    
    if not user:
        # 为了安全，不透露用户是否存在
        return {"message": "如果该邮箱已注册，激活邮件将会发送"}
    
    if user.verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="该邮箱已激活，请直接登录"
        )
    
    # 生成新的激活令牌
    activation_token = generate_activation_token()
    expire_hours = settings.email_activation_expire_hours
    user.activation_token = activation_token
    user.activation_expires_at = datetime.utcnow() + timedelta(hours=expire_hours)
    await db.commit()
    
    # 获取站点名称
    site_name = await get_setting_value(db, "site_name", settings.app_name)
    
    # 后台发送激活邮件
    background_tasks.add_task(
        send_activation_email,
        db,
        user.email,
        activation_token,
        site_name,
        expire_hours
    )
    
    logger.info(f"重新发送激活邮件: {user.email}")
    return {"message": "激活邮件已发送，请查收邮箱"}


@router.get("/captcha")
async def get_captcha(db: AsyncSession = Depends(get_db)):
    """生成图形验证码"""
    import io
    import base64 as b64
    
    # 检查是否启用验证码
    captcha_enabled = await get_setting_value(db, "captcha_enabled", True)
    if not captcha_enabled:
        return {"captcha_id": "", "image_base64": "", "enabled": False}
    
    # 生成验证码(简单实现，不依赖 captcha 库)
    chars = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=4))
    captcha_id = secrets.token_urlsafe(16)
    
    # 存储验证码答案（优先Redis，降级内存）
    await _set_captcha(captcha_id, chars, ttl=300)
    
    # 生成简单的SVG验证码图片
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">
        <rect width="120" height="40" fill="#f0f0f0"/>
        <text x="10" y="30" font-size="28" font-family="Arial" fill="#333"
              transform="rotate(-5,60,20)" letter-spacing="5">{chars}</text>
        <line x1="0" y1="{random.randint(10,30)}" x2="120" y2="{random.randint(10,30)}" stroke="#ccc" stroke-width="1"/>
        <line x1="0" y1="{random.randint(10,30)}" x2="120" y2="{random.randint(10,30)}" stroke="#ddd" stroke-width="1"/>
    </svg>'''
    
    image_base64 = b64.b64encode(svg.encode()).decode()
    
    return {
        "captcha_id": captcha_id,
        "image_base64": f"data:image/svg+xml;base64,{image_base64}",
        "enabled": True,
    }


@router.post("/login", response_model=LoginResponse)
async def login(user_data: UserLogin, request: Request, db: AsyncSession = Depends(get_db)):
    logger.info(f"用户登录请求: {user_data.email}")

    # 速率限制（IP + 邮箱）
    from app.api.v1.audit_log import get_client_ip
    client_ip = get_client_ip(request)
    _check_login_rate(f"ip:{client_ip}", LOGIN_MAX_ATTEMPTS_IP)
    _check_login_rate(f"email:{user_data.email}", LOGIN_MAX_ATTEMPTS_EMAIL)
    
    # 检查验证码（使用 Redis 优先 + 内存降级）
    captcha_enabled = await get_setting_value(db, "captcha_enabled", True)
    if captcha_enabled and user_data.captcha_id:
        stored_answer = await _get_and_delete_captcha(user_data.captcha_id)
        if not stored_answer:
            raise HTTPException(status_code=400, detail="验证码已过期，请刷新")
        if (user_data.captcha_code or "").lower() != stored_answer.lower():
            raise HTTPException(status_code=400, detail="验证码错误")
    
    result = await db.execute(select(User).where(User.email == user_data.email))
    user = result.scalar_one_or_none()
    
    if not user or not verify_password(user_data.password, user.password_hash):
        logger.warning(f"登录失败-用户名或密码错误: {user_data.email}")
        # 记录登录失败日志
        try:
            from app.api.v1.audit_log import create_audit_log, get_client_ip
            from app.models import AuditAction, AuditResourceType
            await create_audit_log(
                db, user.id if user else None, AuditAction.LOGIN_FAILED, AuditResourceType.ACCOUNT,
                resource_name=user_data.email,
                detail="邮箱或密码错误",
                ip_address=get_client_ip(request),
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"记录登录失败日志失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误"
        )
    
    # 检查邮箱是否已激活
    if not user.verified:
        logger.warning(f"登录失败-邮箱未激活: {user_data.email}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="邮箱未激活，请先查收激活邮件完成验证"
        )
    
    access_token = create_access_token(data={"sub": str(user.id)})
    refresh_token = create_refresh_token(data={"sub": str(user.id)})
    
    # 记录登录日志
    try:
        from app.api.v1.audit_log import create_audit_log, get_client_ip
        from app.models import AuditAction, AuditResourceType
        user_agent = request.headers.get("user-agent", "")
        await create_audit_log(
            db, user.id, AuditAction.LOGIN, AuditResourceType.ACCOUNT,
            resource_name=user.email,
            detail=user_agent[:200],
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"记录登录日志失败: {e}")
    
    logger.info(f"用户登录成功: {user.email}, ID: {user.id}")
    return LoginResponse(user=user, token=access_token, refresh_token=refresh_token)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/logout")
async def logout(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 记录登出日志
    try:
        from app.api.v1.audit_log import create_audit_log, get_client_ip
        from app.models import AuditAction, AuditResourceType
        user_agent = request.headers.get("user-agent", "")
        await create_audit_log(
            db, current_user.id, AuditAction.LOGOUT, AuditResourceType.ACCOUNT,
            resource_name=current_user.email,
            detail=user_agent[:200],
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"记录登出日志失败: {e}")
    return {"message": "Successfully logged out"}


@router.post("/refresh")
async def refresh_token(request: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """使用 refresh_token 换取新的 access_token + refresh_token"""
    payload = decode_token(request.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token"
        )
    
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )
    
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    
    new_access_token = create_access_token(data={"sub": str(user.id)})
    new_refresh_token = create_refresh_token(data={"sub": str(user.id)})
    
    logger.info(f"Token刷新成功: {user.email}")
    return {
        "token": new_access_token,
        "refresh_token": new_refresh_token
    }


@router.post("/forgot-password")
async def forgot_password(
    request: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """忘记密码 - 发送密码重置邮件"""
    logger.info(f"密码重置请求: {request.email}")

    # 无论用户是否存在都返回相同消息，防止邮箱枚举
    result = await db.execute(select(User).where(User.email == request.email))
    user = result.scalar_one_or_none()

    if user and user.verified:
        # 生成重置令牌（复用 activation_token 字段，30分钟有效）
        reset_token = secrets.token_urlsafe(32)
        user.activation_token = f"reset:{reset_token}"
        user.activation_expires_at = datetime.utcnow() + timedelta(minutes=30)
        await db.commit()

        site_name = await get_setting_value(db, "site_name", settings.app_name)
        background_tasks.add_task(
            send_password_reset_email, db, request.email, reset_token, site_name, 30
        )
        logger.info(f"密码重置邮件已发送: {request.email}")
    else:
        logger.info(f"密码重置请求-用户不存在或未激活: {request.email}")

    return {"message": "如果该邮箱已注册，重置密码邮件将会发送到您的邮箱"}


@router.post("/reset-password")
async def reset_password(
    request: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db)
):
    """使用重置令牌设置新密码"""
    logger.info("密码重置执行请求")

    # 密码强度校验
    validate_password_strength(request.new_password)

    # 查找持有此重置令牌的用户
    token_value = f"reset:{request.token}"
    result = await db.execute(
        select(User).where(User.activation_token == token_value)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=400, detail="无效的重置链接")

    if user.activation_expires_at and datetime.utcnow() > user.activation_expires_at:
        # 清除过期令牌
        user.activation_token = None
        user.activation_expires_at = None
        await db.commit()
        raise HTTPException(status_code=400, detail="重置链接已过期，请重新申请")

    # 更新密码并清除令牌
    user.password_hash = get_password_hash(request.new_password)
    user.activation_token = None
    user.activation_expires_at = None
    await db.commit()

    logger.info(f"密码重置成功: {user.email}")
    return {"message": "密码重置成功，请使用新密码登录"}


@router.post("/change-password")
async def change_password(
    request: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """修改密码"""
    # 验证旧密码
    if not verify_password(request.old_password, current_user.password_hash):
        logger.warning(f"修改密码失败-旧密码错误: {current_user.email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="旧密码错误"
        )
    
    # 密码强度校验
    validate_password_strength(request.new_password)
    
    # 检查新旧密码是否相同
    if request.old_password == request.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新密码不能与旧密码相同"
        )
    
    # 更新密码
    current_user.password_hash = get_password_hash(request.new_password)
    db.add(current_user)
    await db.commit()
    
    logger.info(f"用户修改密码成功: {current_user.email}")
    return {"message": "密码修改成功"}
