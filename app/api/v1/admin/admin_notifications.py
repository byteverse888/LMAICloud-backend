"""管理端 - 通知管理 API"""
import logging
from typing import Optional, List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, delete

from app.database import get_db
from app.utils.auth import get_current_admin_user
from app.models import Notification, NotificationType, User as AIUser

logger = logging.getLogger(__name__)
router = APIRouter()


class SendNotificationRequest(BaseModel):
    title: str
    content: str = ""
    type: str = "system"
    user_ids: Optional[List[str]] = None  # None 表示发送给所有用户


@router.get("")
async def list_notifications(
    page: int = 1,
    size: int = 20,
    user_email: Optional[str] = None,
    ntype: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """管理端 - 查看所有通知"""
    offset = (page - 1) * size
    conditions = []

    if ntype:
        conditions.append(Notification.type == ntype)
    if user_email:
        sub = select(AIUser.id).where(AIUser.email.ilike(f"%{user_email}%"))
        conditions.append(Notification.user_id.in_(sub))

    count_q = select(func.count(Notification.id))
    if conditions:
        count_q = count_q.where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    q = select(Notification, AIUser.email).join(AIUser, Notification.user_id == AIUser.id)
    if conditions:
        q = q.where(*conditions)
    q = q.order_by(desc(Notification.created_at)).offset(offset).limit(size)

    result = await db.execute(q)
    rows = result.all()

    return {
        "list": [
            {
                "id": str(n.id),
                "user_id": str(n.user_id),
                "user_email": email,
                "title": n.title,
                "content": n.content,
                "type": n.type.value if n.type else "system",
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n, email in rows
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.post("")
async def send_notification(
    req: SendNotificationRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """发送通知给指定用户或全体用户"""
    try:
        ntype = NotificationType(req.type)
    except ValueError:
        ntype = NotificationType.SYSTEM

    if req.user_ids:
        # 发送给指定用户
        user_ids = [UUID(uid) for uid in req.user_ids]
    else:
        # 发送给所有用户
        result = await db.execute(select(AIUser.id).where(AIUser.is_active == True))
        user_ids = [row[0] for row in result.all()]

    count = 0
    for uid in user_ids:
        db.add(Notification(
            user_id=uid,
            title=req.title,
            content=req.content,
            type=ntype,
        ))
        count += 1

    await db.commit()
    logger.info(f"管理员发送通知: title={req.title}, 目标用户数={count}")
    return {"message": f"已发送 {count} 条通知", "count": count}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """删除通知"""
    result = await db.execute(
        select(Notification).where(Notification.id == notification_id)
    )
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(status_code=404, detail="通知不存在")
    await db.delete(notif)
    await db.commit()
    return {"message": "已删除"}
