"""站内通知 API"""
import logging
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, update

from app.database import get_db
from app.utils.auth import get_current_user
from app.models import Notification, NotificationType

logger = logging.getLogger(__name__)
router = APIRouter()


async def create_notification(
    db: AsyncSession,
    user_id,
    title: str,
    content: str = None,
    ntype: NotificationType = NotificationType.SYSTEM,
):
    """创建通知 - 供各模块调用"""
    notif = Notification(
        user_id=user_id,
        title=title,
        content=content,
        type=ntype,
    )
    db.add(notif)
    return notif


@router.get("/")
async def get_notifications(
    page: int = 1,
    size: int = 20,
    unread_only: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """获取通知列表"""
    offset = (page - 1) * size
    conditions = [Notification.user_id == current_user.id]
    if unread_only:
        conditions.append(Notification.is_read == False)

    count_q = select(func.count(Notification.id)).where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    q = (
        select(Notification)
        .where(*conditions)
        .order_by(Notification.is_read, desc(Notification.created_at))
        .offset(offset)
        .limit(size)
    )
    result = await db.execute(q)
    notifs = result.scalars().all()

    return {
        "list": [
            {
                "id": str(n.id),
                "title": n.title,
                "content": n.content,
                "type": n.type.value if n.type else "system",
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in notifs
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/unread-count")
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """获取未读通知数量"""
    q = select(func.count(Notification.id)).where(
        Notification.user_id == current_user.id,
        Notification.is_read == False,
    )
    count = (await db.execute(q)).scalar() or 0
    return {"unread_count": count}


@router.put("/{notification_id}/read")
async def mark_as_read(
    notification_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """标记通知已读"""
    result = await db.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        )
    )
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(status_code=404, detail="通知不存在")
    notif.is_read = True
    await db.commit()
    return {"message": "已标记已读"}


@router.put("/read-all")
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """全部标记已读"""
    await db.execute(
        update(Notification)
        .where(
            Notification.user_id == current_user.id,
            Notification.is_read == False,
        )
        .values(is_read=True)
    )
    await db.commit()
    return {"message": "已全部标记已读"}
