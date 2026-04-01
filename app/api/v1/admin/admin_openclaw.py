"""
OpenClaw 管理端 API - 管理员查看所有用户的 OpenClaw 实例
"""
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models import AIUser as User, OpenClawInstance
from app.utils.auth import get_current_admin_user

router = APIRouter()


@router.get("/instances")
async def admin_list_instances(
    status: str = None,
    search: str = None,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员查看所有 OpenClaw 实例"""
    query = select(OpenClawInstance).where(OpenClawInstance.status != "released")

    if status:
        query = query.where(OpenClawInstance.status == status)
    if search:
        query = query.where(OpenClawInstance.name.ilike(f"%{search}%"))

    query = query.order_by(OpenClawInstance.created_at.desc())
    result = await db.execute(query)
    instances = result.scalars().all()
    return {"list": instances, "total": len(instances)}
