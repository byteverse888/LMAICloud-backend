"""操作日志 API"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, or_

from app.database import get_db
from app.utils.auth import get_current_user, get_current_admin_user
from app.models import AuditLog, AuditAction, AuditResourceType, User as AIUser

logger = logging.getLogger(__name__)
router = APIRouter()


def get_client_ip(request: Request) -> str:
    """获取真实客户端 IP（支持反向代理）

    优先级：X-Forwarded-For 第一个 IP > X-Real-IP > request.client.host
    """
    # X-Forwarded-For: client, proxy1, proxy2
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # 取第一个（原始客户端 IP）
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else "unknown"


async def create_audit_log(
    db: AsyncSession,
    user_id,
    action: AuditAction,
    resource_type: AuditResourceType,
    resource_id: str = None,
    resource_name: str = None,
    detail: str = None,
    ip_address: str = None,
):
    """创建操作日志 - 供各模块调用"""
    log = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_name=resource_name,
        detail=detail,
        ip_address=ip_address,
    )
    db.add(log)
    return log


@router.get("/")
async def get_audit_logs(
    page: int = 1,
    size: int = 20,
    keyword: Optional[str] = None,
    resource_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """用户端 - 查看自己的操作日志"""
    offset = (page - 1) * size

    # 基础条件
    conditions = [AuditLog.user_id == current_user.id]
    if resource_type:
        conditions.append(AuditLog.resource_type == resource_type)
    if keyword:
        conditions.append(
            or_(
                AuditLog.resource_name.ilike(f"%{keyword}%"),
                AuditLog.detail.ilike(f"%{keyword}%"),
            )
        )

    count_q = select(func.count(AuditLog.id)).where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    q = (
        select(AuditLog)
        .where(*conditions)
        .order_by(desc(AuditLog.created_at))
        .offset(offset)
        .limit(size)
    )
    result = await db.execute(q)
    logs = result.scalars().all()

    return {
        "list": [
            {
                "id": str(l.id),
                "action": l.action.value if l.action else "",
                "resource_type": l.resource_type.value if l.resource_type else "",
                "resource_id": l.resource_id,
                "resource_name": l.resource_name,
                "detail": l.detail,
                "ip_address": l.ip_address,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/access-log")
async def get_access_logs(
    page: int = 1,
    size: int = 20,
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """用户端 - 查看自己的登录/访问记录"""
    from datetime import datetime, timedelta
    offset = (page - 1) * size
    cutoff = datetime.utcnow() - timedelta(days=days)

    conditions = [
        AuditLog.user_id == current_user.id,
        or_(AuditLog.action == AuditAction.LOGIN, AuditLog.action == AuditAction.LOGOUT),
        AuditLog.created_at >= cutoff,
    ]

    count_q = select(func.count(AuditLog.id)).where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    q = (
        select(AuditLog)
        .where(*conditions)
        .order_by(desc(AuditLog.created_at))
        .offset(offset)
        .limit(size)
    )
    result = await db.execute(q)
    logs = result.scalars().all()

    return {
        "list": [
            {
                "id": str(l.id),
                "action": l.action.value if l.action else "login",
                "ip_address": l.ip_address or "",
                "device": l.detail or "",
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/admin")
async def get_admin_audit_logs(
    page: int = 1,
    size: int = 20,
    user_email: Optional[str] = None,
    keyword: Optional[str] = None,
    resource_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """管理端 - 查看所有操作日志"""
    offset = (page - 1) * size

    conditions = []
    if resource_type:
        conditions.append(AuditLog.resource_type == resource_type)
    if keyword:
        conditions.append(
            or_(
                AuditLog.resource_name.ilike(f"%{keyword}%"),
                AuditLog.detail.ilike(f"%{keyword}%"),
            )
        )
    if user_email:
        sub = select(AIUser.id).where(AIUser.email.ilike(f"%{user_email}%"))
        conditions.append(AuditLog.user_id.in_(sub))

    count_q = select(func.count(AuditLog.id))
    if conditions:
        count_q = count_q.where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    q = select(AuditLog, AIUser.email).join(AIUser, AuditLog.user_id == AIUser.id)
    if conditions:
        q = q.where(*conditions)
    q = q.order_by(desc(AuditLog.created_at)).offset(offset).limit(size)

    result = await db.execute(q)
    rows = result.all()

    return {
        "list": [
            {
                "id": str(log.id),
                "user_id": str(log.user_id),
                "user_email": email,
                "action": log.action.value if log.action else "",
                "resource_type": log.resource_type.value if log.resource_type else "",
                "resource_id": log.resource_id,
                "resource_name": log.resource_name,
                "detail": log.detail,
                "ip_address": log.ip_address,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log, email in rows
        ],
        "total": total,
        "page": page,
        "size": size,
    }
