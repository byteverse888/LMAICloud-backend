"""管理端 - 推广管理 API"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.database import get_db
from app.utils.auth import get_current_admin_user
from app.models import User as AIUser, PointRecord, PointType

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/stats")
async def get_referral_stats(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """推广统计总览"""
    # 总邀请人数（invited_by 不为空的用户数）
    total_invited = (await db.execute(
        select(func.count(AIUser.id)).where(AIUser.invited_by.isnot(None))
    )).scalar() or 0

    # 参与推广的用户数（有邀请码的用户数）
    total_referrers = (await db.execute(
        select(func.count(AIUser.id)).where(AIUser.invite_code.isnot(None))
    )).scalar() or 0

    # 推广产生的总积分
    total_points = (await db.execute(
        select(func.sum(PointRecord.points)).where(
            PointRecord.type == PointType.INVITE_REWARD
        )
    )).scalar() or 0

    return {
        "total_invited": total_invited,
        "total_referrers": total_referrers,
        "total_reward_points": total_points,
    }


@router.get("/records")
async def get_referral_records(
    page: int = 1,
    size: int = 20,
    referrer_email: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """全部推广记录（含邀请人和被邀请人信息）"""
    offset = (page - 1) * size

    # 被邀请人表
    InvitedUser = AIUser.__table__.alias("invited")
    ReferrerUser = AIUser.__table__.alias("referrer")

    conditions = [AIUser.invited_by.isnot(None)]
    if referrer_email:
        sub = select(AIUser.id).where(AIUser.email.ilike(f"%{referrer_email}%"))
        conditions.append(AIUser.invited_by.in_(sub))

    # 总数
    count_q = select(func.count(AIUser.id)).where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    # 列表：被邀请用户 + 邀请人信息
    q = (
        select(AIUser)
        .where(*conditions)
        .order_by(desc(AIUser.created_at))
        .offset(offset)
        .limit(size)
    )
    result = await db.execute(q)
    invited_users = result.scalars().all()

    records = []
    for u in invited_users:
        # 查询邀请人
        referrer = None
        if u.invited_by:
            ref_result = await db.execute(
                select(AIUser.email, AIUser.nickname).where(AIUser.id == u.invited_by)
            )
            ref_row = ref_result.first()
            if ref_row:
                referrer = {"email": ref_row[0], "nickname": ref_row[1]}

        records.append({
            "invited_user_email": u.email,
            "invited_user_nickname": u.nickname,
            "invited_at": u.created_at.isoformat() if u.created_at else None,
            "verified": u.verified,
            "referrer_email": referrer["email"] if referrer else "--",
            "referrer_nickname": referrer["nickname"] if referrer else "--",
            "reward_points": 50 if u.verified else 0,
        })

    return {
        "list": records,
        "total": total,
        "page": page,
        "size": size,
    }
