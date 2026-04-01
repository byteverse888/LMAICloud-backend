"""推广邀请 API"""
import logging
import secrets
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.database import get_db
from app.utils.auth import get_current_user
from app.models import User as AIUser

logger = logging.getLogger(__name__)
router = APIRouter()


def generate_invite_code() -> str:
    """生成8位随机邀请码"""
    return secrets.token_urlsafe(6)[:8].upper()


@router.get("/info")
async def get_referral_info(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """获取我的推广信息"""
    # 如果还没有邀请码，自动生成
    if not current_user.invite_code:
        current_user.invite_code = generate_invite_code()
        await db.commit()

    # 统计已邀请人数
    count_q = select(func.count(AIUser.id)).where(AIUser.invited_by == current_user.id)
    invited_count = (await db.execute(count_q)).scalar() or 0

    return {
        "invite_code": current_user.invite_code,
        "invite_link": f"/register?invite={current_user.invite_code}",
        "invited_count": invited_count,
        "points_per_invite": 50,
    }


@router.get("/records")
async def get_referral_records(
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """获取邀请记录"""
    offset = (page - 1) * size

    count_q = select(func.count(AIUser.id)).where(AIUser.invited_by == current_user.id)
    total = (await db.execute(count_q)).scalar() or 0

    q = (
        select(AIUser)
        .where(AIUser.invited_by == current_user.id)
        .order_by(desc(AIUser.created_at))
        .offset(offset)
        .limit(size)
    )
    result = await db.execute(q)
    users = result.scalars().all()

    records = []
    for u in users:
        email = u.email
        # 脱敏: a***b@example.com
        at_idx = email.index("@") if "@" in email else len(email)
        if at_idx > 2:
            masked_email = email[0] + "***" + email[at_idx - 1:]
        else:
            masked_email = email[0] + "***" + email[at_idx:]

        records.append({
            "user_email": masked_email,
            "registered_at": u.created_at.isoformat() if u.created_at else None,
            "reward_points": 50,
        })

    return {
        "list": records,
        "total": total,
        "page": page,
        "size": size,
    }
