from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.schemas import UserResponse
from app.utils.auth import get_current_user

router = APIRouter()


class UserProfileUpdate(BaseModel):
    nickname: Optional[str] = None
    phone: Optional[str] = None
    avatar: Optional[str] = None


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """获取当前用户完整信息（前端通用接口）"""
    return current_user


@router.patch("/me", response_model=UserResponse)
async def patch_me(
    data: UserProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """更新当前用户资料"""
    if data.nickname is not None:
        current_user.nickname = data.nickname
    if data.phone is not None:
        current_user.phone = data.phone
    if data.avatar is not None:
        current_user.avatar = data.avatar
    await db.commit()
    await db.refresh(current_user)
    return current_user


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
