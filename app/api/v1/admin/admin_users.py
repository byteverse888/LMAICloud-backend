"""用户管理 API (管理端)"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from typing import List, Optional
from datetime import datetime
from uuid import UUID

from app.database import get_db
from app.models import User, UserRole, UserStatus, Instance, Order, InstanceStatus
from app.schemas import UserResponse, UserCreate
from app.utils.auth import get_current_admin_user, get_password_hash
from app.logging_config import get_logger

router = APIRouter()
logger = get_logger("lmaicloud.admin.users")


@router.get("/")
async def list_users(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取用户列表"""
    # 统计总数
    count_query = select(func.count(User.id))
    if search:
        count_query = count_query.where(
            or_(
                User.email.ilike(f"%{search}%"),
                User.nickname.ilike(f"%{search}%"),
            )
        )
    if status:
        try:
            count_query = count_query.where(User.status == UserStatus(status))
        except ValueError:
            pass
    
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    
    # 分页查询
    query = select(User)
    if search:
        query = query.where(
            or_(
                User.email.ilike(f"%{search}%"),
                User.nickname.ilike(f"%{search}%"),
            )
        )
    if status:
        try:
            query = query.where(User.status == UserStatus(status))
        except ValueError:
            pass
    
    skip = (page - 1) * size
    query = query.order_by(User.created_at.desc()).offset(skip).limit(size)
    result = await db.execute(query)
    users = result.scalars().all()
    
    # 获取用户实例数
    user_list = []
    for user in users:
        instance_count = await db.execute(
            select(func.count(Instance.id)).where(Instance.user_id == user.id)
        )
        user_list.append({
            "id": str(user.id),
            "email": user.email,
            "nickname": user.nickname or user.email.split('@')[0],
            "balance": float(user.balance or 0),
            "status": user.status.value if hasattr(user.status, 'value') else str(user.status),
            "verified": user.verified if hasattr(user, 'verified') else False,
            "instances": instance_count.scalar() or 0,
            "created_at": user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "",
        })
    
    return {"list": user_list, "total": total}


@router.get("/stats")
async def get_user_stats(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取用户统计信息"""
    # 用户总数
    total_count = await db.execute(select(func.count(User.id)))
    total_users = total_count.scalar()
    
    # 活跃用户
    active_count = await db.execute(
        select(func.count(User.id)).where(User.status == UserStatus.ACTIVE)
    )
    active_users = active_count.scalar()
    
    # 今日新增
    today = datetime.utcnow().date()
    today_count = await db.execute(
        select(func.count(User.id)).where(
            func.date(User.created_at) == today
        )
    )
    today_users = today_count.scalar()
    
    return {
        "total_users": total_users,
        "active_users": active_users,
        "inactive_users": total_users - active_users,
        "today_new_users": today_users,
    }


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取用户详情"""
    result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


@router.get("/{user_id}/instances")
async def get_user_instances(
    user_id: UUID,
    skip: int = 0,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取用户的实例列表"""
    result = await db.execute(
        select(Instance).where(Instance.user_id == user_id)
        .offset(skip).limit(limit)
    )
    instances = result.scalars().all()
    return instances


@router.post("/", response_model=UserResponse)
async def create_user(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """创建用户 (管理端)"""
    # 检查邮箱是否存在
    existing = await db.execute(
        select(User).where(User.email == user_data.email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="邮箱已被注册")
    
    user = User(
        email=user_data.email,
        nickname=user_data.nickname or user_data.email.split('@')[0],
        password_hash=get_password_hash(user_data.password),
        role=UserRole.ADMIN if user_data.role == "admin" else UserRole.USER,
        status=UserStatus.ACTIVE,
        verified=True,  # 管理员添加的用户无需邮箱激活
        balance=0.0,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.put("/{user_id}/status")
async def update_user_status(
    user_id: UUID,
    status: str = Query(..., regex="^(active|inactive|banned)$"),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """更新用户状态"""
    result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    user.status = status
    user.updated_at = datetime.utcnow()
    await db.commit()
    return {"message": f"用户状态已更新为 {status}"}


@router.put("/{user_id}/balance")
async def adjust_user_balance(
    user_id: UUID,
    amount: float,
    reason: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """调整用户余额"""
    result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    user.balance += amount
    user.updated_at = datetime.utcnow()
    await db.commit()
    
    return {
        "message": "余额调整成功",
        "new_balance": user.balance,
        "adjustment": amount,
        "reason": reason,
    }


@router.put("/{user_id}/role")
async def update_user_role(
    user_id: UUID,
    role: str = Query(..., regex="^(user|admin)$"),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """更新用户角色"""
    result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    user.role = role
    user.updated_at = datetime.utcnow()
    await db.commit()
    return {"message": f"用户角色已更新为 {role}"}


@router.delete("/{user_id}")
async def delete_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """删除用户"""
    logger.info(f"删除用户请求 - 目标用户: {user_id}, 操作者: {current_user.id}")
    
    try:
        result = await db.execute(
            select(User).where(User.id == user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            logger.warning(f"删除用户失败 - 用户不存在: {user_id}")
            raise HTTPException(status_code=404, detail="用户不存在")
        
        # 检查是否有运行中的实例
        instance_count = await db.execute(
            select(func.count(Instance.id)).where(
                Instance.user_id == user_id,
                Instance.status.in_([InstanceStatus.RUNNING, InstanceStatus.CREATING, InstanceStatus.STARTING])
            )
        )
        running_count = instance_count.scalar() or 0
        if running_count > 0:
            logger.warning(f"删除用户失败 - 用户有运行中实例: {user_id}, 实例数: {running_count}")
            raise HTTPException(status_code=400, detail="用户有运行中的实例，无法删除")
        
        user_email = user.email
        await db.delete(user)
        await db.commit()
        logger.info(f"删除用户成功 - 用户: {user_id}, 邮箱: {user_email}")
        return {"message": "用户已删除"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除用户异常 - 用户: {user_id}, 错误: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="删除用户失败")
