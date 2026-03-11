from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.schemas import UserResponse
from app.utils.auth import get_current_user

router = APIRouter()


@router.get("/profile", response_model=UserResponse)
async def get_profile(current_user: User = Depends(get_current_user)):
    return current_user


@router.put("/profile")
async def update_profile(
    nickname: str = None,
    avatar: str = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if nickname:
        current_user.nickname = nickname
    if avatar:
        current_user.avatar = avatar
    
    await db.commit()
    await db.refresh(current_user)
    
    return current_user


@router.get("/balance")
async def get_balance(current_user: User = Depends(get_current_user)):
    return {
        "available": current_user.balance,
        "frozen": current_user.frozen_balance,
        "coupon": 0,
        "voucher": 0
    }
