"""订单管理 API (管理端)"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List, Optional
from datetime import datetime, timedelta
from uuid import UUID

from app.database import get_db
from app.models import Order, User, Recharge
from app.schemas import OrderResponse, RechargeResponse
from app.utils.auth import get_current_admin_user

router = APIRouter()


@router.get("/consumption", response_model=List[OrderResponse])
async def list_consumption_orders(
    skip: int = 0,
    limit: int = 20,
    user_id: Optional[UUID] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取消费订单列表"""
    query = select(Order)
    
    if user_id:
        query = query.where(Order.user_id == user_id)
    if status:
        query = query.where(Order.status == status)
    if start_date:
        query = query.where(Order.created_at >= datetime.fromisoformat(start_date))
    if end_date:
        query = query.where(Order.created_at <= datetime.fromisoformat(end_date))
    
    query = query.order_by(Order.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    orders = result.scalars().all()
    return orders


@router.get("/recharge", response_model=List[RechargeResponse])
async def list_recharge_orders(
    skip: int = 0,
    limit: int = 20,
    user_id: Optional[UUID] = None,
    status: Optional[str] = None,
    payment_method: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取充值订单列表"""
    query = select(Recharge)
    
    if user_id:
        query = query.where(Recharge.user_id == user_id)
    if status:
        query = query.where(Recharge.status == status)
    if payment_method:
        query = query.where(Recharge.payment_method == payment_method)
    
    query = query.order_by(Recharge.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    recharges = result.scalars().all()
    return recharges


@router.get("/stats")
async def get_order_stats(
    days: int = 30,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取订单统计信息"""
    start_date = datetime.utcnow() - timedelta(days=days)
    
    # 消费总额
    consumption_total = await db.execute(
        select(func.sum(Order.amount)).where(
            Order.status == "completed",
            Order.created_at >= start_date,
        )
    )
    
    # 充值总额
    recharge_total = await db.execute(
        select(func.sum(Recharge.amount)).where(
            Recharge.status == "completed",
            Recharge.created_at >= start_date,
        )
    )
    
    # 订单数量
    order_count = await db.execute(
        select(func.count(Order.id)).where(Order.created_at >= start_date)
    )
    
    # 充值订单数量
    recharge_count = await db.execute(
        select(func.count(Recharge.id)).where(Recharge.created_at >= start_date)
    )
    
    return {
        "period_days": days,
        "total_consumption": consumption_total.scalar() or 0,
        "total_recharge": recharge_total.scalar() or 0,
        "consumption_orders": order_count.scalar(),
        "recharge_orders": recharge_count.scalar(),
    }


@router.get("/consumption/{order_id}", response_model=OrderResponse)
async def get_consumption_order(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取消费订单详情"""
    result = await db.execute(
        select(Order).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


@router.get("/recharge/{recharge_id}", response_model=RechargeResponse)
async def get_recharge_order(
    recharge_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取充值订单详情"""
    result = await db.execute(
        select(Recharge).where(Recharge.id == recharge_id)
    )
    recharge = result.scalar_one_or_none()
    if not recharge:
        raise HTTPException(status_code=404, detail="充值订单不存在")
    return recharge


@router.put("/recharge/{recharge_id}/confirm")
async def confirm_recharge_order(
    recharge_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """确认充值订单 (手动充值)"""
    result = await db.execute(
        select(Recharge).where(Recharge.id == recharge_id)
    )
    recharge = result.scalar_one_or_none()
    if not recharge:
        raise HTTPException(status_code=404, detail="充值订单不存在")
    
    if recharge.status != "pending":
        raise HTTPException(status_code=400, detail="订单状态不允许确认")
    
    # 更新充值订单状态
    recharge.status = "completed"
    recharge.completed_at = datetime.utcnow()
    
    # 更新用户余额
    user_result = await db.execute(
        select(User).where(User.id == recharge.user_id)
    )
    user = user_result.scalar_one_or_none()
    if user:
        user.balance += recharge.amount
    
    await db.commit()
    return {"message": "充值订单已确认", "amount": recharge.amount}


@router.put("/recharge/{recharge_id}/cancel")
async def cancel_recharge_order(
    recharge_id: UUID,
    reason: str = "",
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """取消充值订单"""
    result = await db.execute(
        select(Recharge).where(Recharge.id == recharge_id)
    )
    recharge = result.scalar_one_or_none()
    if not recharge:
        raise HTTPException(status_code=404, detail="充值订单不存在")
    
    if recharge.status not in ["pending"]:
        raise HTTPException(status_code=400, detail="订单状态不允许取消")
    
    recharge.status = "cancelled"
    await db.commit()
    return {"message": "充值订单已取消"}


@router.put("/consumption/{order_id}/refund")
async def refund_consumption_order(
    order_id: UUID,
    reason: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """退款消费订单"""
    result = await db.execute(
        select(Order).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    
    if order.status != "completed":
        raise HTTPException(status_code=400, detail="订单状态不允许退款")
    
    # 更新订单状态
    order.status = "refunded"
    
    # 退还用户余额
    user_result = await db.execute(
        select(User).where(User.id == order.user_id)
    )
    user = user_result.scalar_one_or_none()
    if user:
        user.balance += order.amount
    
    await db.commit()
    return {"message": "订单已退款", "refund_amount": order.amount}
