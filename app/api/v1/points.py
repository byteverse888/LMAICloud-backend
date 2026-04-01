"""积分系统 API"""
import logging
from datetime import datetime, date
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.database import get_db
from app.utils.auth import get_current_user
from app.models import User as AIUser, PointRecord, PointType

logger = logging.getLogger(__name__)
router = APIRouter()


async def add_points(db: AsyncSession, user_id, points: int, point_type: PointType, description: str):
    """通用积分发放函数，供其他模块调用"""
    # 创建积分流水
    record = PointRecord(
        user_id=user_id,
        points=points,
        type=point_type,
        description=description,
    )
    db.add(record)
    # 更新用户积分余额
    result = await db.execute(select(AIUser).where(AIUser.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.points = (user.points or 0) + points
    return record


@router.get("/balance")
async def get_points_balance(
    current_user=Depends(get_current_user),
):
    """获取积分余额"""
    return {"points": current_user.points or 0}


@router.get("/records")
async def get_point_records(
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """获取积分流水"""
    offset = (page - 1) * size

    # 总数
    count_q = select(func.count(PointRecord.id)).where(PointRecord.user_id == current_user.id)
    total = (await db.execute(count_q)).scalar() or 0

    # 列表
    q = (
        select(PointRecord)
        .where(PointRecord.user_id == current_user.id)
        .order_by(desc(PointRecord.created_at))
        .offset(offset)
        .limit(size)
    )
    result = await db.execute(q)
    records = result.scalars().all()

    return {
        "list": [
            {
                "id": str(r.id),
                "points": r.points,
                "type": r.type.value if r.type else "",
                "description": r.description,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.post("/daily-checkin")
async def daily_checkin(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """每日签到 +2积分"""
    today = date.today().isoformat()

    if current_user.last_checkin_date == today:
        raise HTTPException(status_code=400, detail="今日已签到")

    # 发放积分
    await add_points(db, current_user.id, 2, PointType.DAILY_LOGIN, f"每日签到奖励 ({today})")

    # 更新签到日期
    current_user.last_checkin_date = today
    await db.commit()

    return {
        "message": "签到成功，获得2积分",
        "points": (current_user.points or 0),
        "checkin_date": today,
    }
