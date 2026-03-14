from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr
import random
import string
import secrets
from datetime import datetime, timedelta

from app.database import get_db
from app.models import User, SystemSetting
from app.schemas import UserCreate, UserLogin, UserResponse, LoginResponse
from app.utils.auth import get_password_hash, verify_password, create_access_token, create_refresh_token, decode_token, get_current_user
from app.logging_config import get_logger
from app.config import settings
from app.services.email_service import send_activation_email, get_email_config
import json

router = APIRouter()
logger = get_logger("lmaicloud.auth")

# 简易验证码存储（生产环境应使用Redis）
verify_codes: dict[str, dict] = {}


class SendCodeRequest(BaseModel):
    email: EmailStr


class CodeLoginRequest(BaseModel):
    email: EmailStr
    code: str


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
    db: AsyncSession = Depends(get_db)
):
    """用户注册 - 发送激活邮件"""
    logger.info(f"用户注册请求: {user_data.email}")
    
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
            site_name = await get_setting_value(db, "site_name", "LMAICloud")
            
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
    
    db.add(user)
    await db.commit()
    await db.refresh(user)
    
    logger.info(f"用户注册成功: {user.email}, ID: {user.id}")
    
    # 如果需要邮箱验证，发送激活邮件
    if email_verification_required:
        # 获取站点名称
        site_name = await get_setting_value(db, "site_name", "LMAICloud")
        
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
    site_name = await get_setting_value(db, "site_name", "LMAICloud")
    
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


@router.post("/login", response_model=LoginResponse)
async def login(user_data: UserLogin, db: AsyncSession = Depends(get_db)):
    logger.info(f"用户登录请求: {user_data.email}")
    
    result = await db.execute(select(User).where(User.email == user_data.email))
    user = result.scalar_one_or_none()
    
    if not user or not verify_password(user_data.password, user.password_hash):
        logger.warning(f"登录失败-用户名或密码错误: {user_data.email}")
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
    
    logger.info(f"用户登录成功: {user.email}, ID: {user.id}")
    return LoginResponse(user=user, token=access_token, refresh_token=refresh_token)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/logout")
async def logout():
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
async def forgot_password(email: str, db: AsyncSession = Depends(get_db)):
    # TODO: Implement password reset email
    return {"message": "Password reset email sent"}


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
    
    # 检查新密码长度
    if len(request.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新密码长度至少6位"
        )
    
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
