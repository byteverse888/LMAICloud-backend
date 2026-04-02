"""订单管理 API (管理端)

包含：消费订单、充值订单、交易流水、账单统计、资源套餐 CRUD
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, extract
from typing import List, Optional
from datetime import datetime, timedelta
from uuid import UUID

from app.database import get_db
from app.models import Order, OrderStatus, User, Recharge, RechargeStatus, ResourcePlan
from app.schemas import (
    OrderResponse, RechargeResponse,
    ResourcePlanCreate, ResourcePlanUpdate, ResourcePlanResponse,
)
from app.utils.auth import get_current_admin_user

router = APIRouter()


# ==================== 统一订单列表 ====================

@router.get("")
async def list_all_orders(
    page: int = 1,
    size: int = 20,
    user_id: Optional[UUID] = None,
    user_email: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取所有订单列表（合并消费+充值）"""
    # 找到目标用户
    target_user_ids = None
    if user_email:
        res = await db.execute(select(User.id).where(User.email.ilike(f"%{user_email}%")))
        target_user_ids = [r[0] for r in res.all()]
        if not target_user_ids:
            return {"list": [], "total": 0, "page": page, "size": size}

    all_orders = []

    # 消费订单
    oq = select(Order)
    if user_id:
        oq = oq.where(Order.user_id == user_id)
    if target_user_ids:
        oq = oq.where(Order.user_id.in_(target_user_ids))
    if status and status != "all":
        oq = oq.where(Order.status == status)
    orders = (await db.execute(oq.order_by(Order.created_at.desc()))).scalars().all()

    for o in orders:
        # 查询用户邮箱
        user_res = await db.execute(select(User.email).where(User.id == o.user_id))
        email = user_res.scalar() or ""
        o_type = o.type.value if hasattr(o.type, 'value') else str(o.type or 'consumption')
        o_status = o.status.value if hasattr(o.status, 'value') else str(o.status or 'pending')
        all_orders.append({
            "id": str(o.id),
            "user_id": str(o.user_id),
            "user_email": email,
            "type": o_type,
            "product_name": o.product_name or o.description or "",
            "description": o.description or "",
            "amount": float(o.amount or 0),
            "status": o_status,
            "created_at": o.created_at.isoformat() if o.created_at else "",
        })

    # 充值订单
    rq = select(Recharge)
    if user_id:
        rq = rq.where(Recharge.user_id == user_id)
    if target_user_ids:
        rq = rq.where(Recharge.user_id.in_(target_user_ids))
    if status and status != "all":
        # 将前端统一状态映射到 RechargeStatus
        recharge_status_map = {"paid": RechargeStatus.SUCCESS, "pending": RechargeStatus.PENDING, "failed": RechargeStatus.FAILED}
        mapped = recharge_status_map.get(status)
        if mapped:
            rq = rq.where(Recharge.status == mapped)
        else:
            rq = rq.where(Recharge.status == status)
    recharges = (await db.execute(rq.order_by(Recharge.created_at.desc()))).scalars().all()

    for r in recharges:
        user_res = await db.execute(select(User.email).where(User.id == r.user_id))
        email = user_res.scalar() or ""
        # 将 RechargeStatus 归一化为 OrderStatus 值（success→paid, failed→cancelled）
        raw_status = r.status.value if hasattr(r.status, 'value') else str(r.status)
        status_map = {"success": "paid", "failed": "cancelled"}
        normalized_status = status_map.get(raw_status, raw_status)
        payment = r.payment_method.value if hasattr(r.payment_method, 'value') else str(r.payment_method or '')
        all_orders.append({
            "id": str(r.id),
            "user_id": str(r.user_id),
            "user_email": email,
            "type": "recharge",
            "product_name": f"{payment}充值",
            "description": f"{payment}充值",
            "amount": float(r.amount or 0),
            "status": normalized_status,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        })

    # 按时间排序
    all_orders.sort(key=lambda x: x["created_at"], reverse=True)
    total = len(all_orders)
    start = (page - 1) * size
    end_idx = start + size

    return {"list": all_orders[start:end_idx], "total": total, "page": page, "size": size}


# ==================== 消费订单 ====================

@router.get("/consumption", response_model=List[OrderResponse])
async def list_consumption_orders(
    skip: int = 0,
    limit: int = 20,
    user_id: Optional[UUID] = None,
    user_email: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取消费订单列表（支持用户筛选）"""
    query = select(Order)

    if user_id:
        query = query.where(Order.user_id == user_id)
    if user_email:
        # 通过 email 模糊匹配找到用户
        sub = select(User.id).where(User.email.ilike(f"%{user_email}%"))
        query = query.where(Order.user_id.in_(sub))
    if status:
        query = query.where(Order.status == status)
    if start_date:
        query = query.where(Order.created_at >= datetime.fromisoformat(start_date))
    if end_date:
        query = query.where(Order.created_at <= datetime.fromisoformat(end_date))

    # 获取总数
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(Order.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    orders = result.scalars().all()
    return orders


@router.get("/consumption/total")
async def get_consumption_total(
    user_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取消费订单总数（用于分页）"""
    query = select(func.count(Order.id))
    if user_id:
        query = query.where(Order.user_id == user_id)
    total = (await db.execute(query)).scalar() or 0
    return {"total": total}


# ==================== 充值订单 ====================

@router.get("/recharge", response_model=List[RechargeResponse])
async def list_recharge_orders(
    skip: int = 0,
    limit: int = 20,
    user_id: Optional[UUID] = None,
    user_email: Optional[str] = None,
    status: Optional[str] = None,
    payment_method: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取充值订单列表（支持用户筛选）"""
    query = select(Recharge)

    if user_id:
        query = query.where(Recharge.user_id == user_id)
    if user_email:
        sub = select(User.id).where(User.email.ilike(f"%{user_email}%"))
        query = query.where(Recharge.user_id.in_(sub))
    if status:
        query = query.where(Recharge.status == status)
    if payment_method:
        query = query.where(Recharge.payment_method == payment_method)

    query = query.order_by(Recharge.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


# ==================== 统计 ====================

@router.get("/stats")
async def get_order_stats(
    days: int = 30,
    user_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取订单统计信息（支持按用户筛选）"""
    start_date = datetime.utcnow() - timedelta(days=days)

    # 消费总额
    cq = select(func.sum(Order.amount)).where(
        Order.status == OrderStatus.PAID, Order.created_at >= start_date
    )
    if user_id:
        cq = cq.where(Order.user_id == user_id)
    consumption_total = (await db.execute(cq)).scalar() or 0

    # 充值总额
    rq = select(func.sum(Recharge.amount)).where(
        Recharge.status == RechargeStatus.SUCCESS, Recharge.created_at >= start_date
    )
    if user_id:
        rq = rq.where(Recharge.user_id == user_id)
    recharge_total = (await db.execute(rq)).scalar() or 0

    # 订单数量
    oq = select(func.count(Order.id)).where(Order.created_at >= start_date)
    if user_id:
        oq = oq.where(Order.user_id == user_id)
    order_count = (await db.execute(oq)).scalar() or 0

    # 充值订单数量
    rcq = select(func.count(Recharge.id)).where(Recharge.created_at >= start_date)
    if user_id:
        rcq = rcq.where(Recharge.user_id == user_id)
    recharge_count = (await db.execute(rcq)).scalar() or 0

    return {
        "period_days": days,
        "total_consumption": float(consumption_total),
        "total_recharge": float(recharge_total),
        "consumption_orders": order_count,
        "recharge_orders": recharge_count,
    }


# ==================== 交易流水 ====================

@router.get("/transactions")
async def list_admin_transactions(
    page: int = 1,
    size: int = 20,
    user_id: Optional[UUID] = None,
    user_email: Optional[str] = None,
    type: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """管理端交易流水（合并消费+充值），支持用户筛选"""
    # 如果按 email 筛选，先查 user_id
    target_user_ids = None
    if user_email:
        res = await db.execute(select(User.id).where(User.email.ilike(f"%{user_email}%")))
        target_user_ids = [r[0] for r in res.all()]
        if not target_user_ids:
            return {"list": [], "total": 0, "page": page, "size": size}

    transactions = []

    # 消费
    if type in (None, "consumption"):
        oq = select(Order).order_by(Order.created_at.desc())
        if user_id:
            oq = oq.where(Order.user_id == user_id)
        if target_user_ids:
            oq = oq.where(Order.user_id.in_(target_user_ids))
        orders = (await db.execute(oq)).scalars().all()
        for o in orders:
            o_type = o.type.value if hasattr(o.type, 'value') else str(o.type or 'consumption')
            o_status = o.status.value if hasattr(o.status, 'value') else str(o.status or 'pending')
            transactions.append({
                "id": str(o.id),
                "user_id": str(o.user_id),
                "type": "consumption",
                "amount": -abs(o.amount),
                "status": o_status,
                "created_at": o.created_at.isoformat(),
                "description": o.description or o.product_name or f"{o_type}订单",
            })

    # 充值
    if type in (None, "recharge"):
        rq = select(Recharge).where(Recharge.status == RechargeStatus.SUCCESS).order_by(Recharge.created_at.desc())
        if user_id:
            rq = rq.where(Recharge.user_id == user_id)
        if target_user_ids:
            rq = rq.where(Recharge.user_id.in_(target_user_ids))
        recharges = (await db.execute(rq)).scalars().all()
        for r in recharges:
            payment = r.payment_method.value if hasattr(r.payment_method, 'value') else str(r.payment_method or '')
            transactions.append({
                "id": str(r.id),
                "user_id": str(r.user_id),
                "type": "recharge",
                "amount": r.amount,
                "status": "paid",
                "created_at": r.created_at.isoformat(),
                "description": f"{payment}充值",
            })

    transactions.sort(key=lambda x: x["created_at"], reverse=True)
    total = len(transactions)
    start = (page - 1) * size
    end_idx = start + size

    return {"list": transactions[start:end_idx], "total": total, "page": page, "size": size}


# ==================== 账单统计 ====================

@router.get("/statements")
async def list_admin_statements(
    year: Optional[int] = None,
    user_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """管理端按月账单统计（支持用户筛选）"""
    if not year:
        year = datetime.utcnow().year

    # 消费汇总
    cq = select(
        extract("month", Order.created_at).label("month"),
        func.sum(func.abs(Order.amount)).label("total"),
        func.count(Order.id).label("count"),
    ).where(extract("year", Order.created_at) == year)
    if user_id:
        cq = cq.where(Order.user_id == user_id)
    cq = cq.group_by(extract("month", Order.created_at))
    c_rows = (await db.execute(cq)).all()
    c_map = {int(r.month): {"total": float(r.total or 0), "count": int(r.count)} for r in c_rows}

    # 充值汇总
    rq = select(
        extract("month", Recharge.created_at).label("month"),
        func.sum(Recharge.amount).label("total"),
        func.count(Recharge.id).label("count"),
    ).where(
        Recharge.status == RechargeStatus.SUCCESS,
        extract("year", Recharge.created_at) == year,
    )
    if user_id:
        rq = rq.where(Recharge.user_id == user_id)
    rq = rq.group_by(extract("month", Recharge.created_at))
    r_rows = (await db.execute(rq)).all()
    r_map = {int(r.month): {"total": float(r.total or 0), "count": int(r.count)} for r in r_rows}

    statements = []
    for m in range(1, 13):
        c = c_map.get(m, {"total": 0, "count": 0})
        r = r_map.get(m, {"total": 0, "count": 0})
        statements.append({
            "month": m, "year": year,
            "consumption": c["total"], "consumption_count": c["count"],
            "recharge": r["total"], "recharge_count": r["count"],
            "net": r["total"] - c["total"],
        })

    return {"year": year, "statements": statements}


# ==================== 订单详情 / 操作 ====================

@router.get("/consumption/{order_id}", response_model=OrderResponse)
async def get_consumption_order(
    order_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取消费订单详情"""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


@router.get("/recharge/{recharge_id}", response_model=RechargeResponse)
async def get_recharge_order(
    recharge_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取充值订单详情"""
    result = await db.execute(select(Recharge).where(Recharge.id == recharge_id))
    recharge = result.scalar_one_or_none()
    if not recharge:
        raise HTTPException(status_code=404, detail="充值订单不存在")
    return recharge


@router.put("/recharge/{recharge_id}/confirm")
async def confirm_recharge_order(
    recharge_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """确认充值订单 (手动充值)"""
    result = await db.execute(select(Recharge).where(Recharge.id == recharge_id))
    recharge = result.scalar_one_or_none()
    if not recharge:
        raise HTTPException(status_code=404, detail="充值订单不存在")
    if recharge.status != RechargeStatus.PENDING:
        raise HTTPException(status_code=400, detail="订单状态不允许确认")

    recharge.status = RechargeStatus.SUCCESS
    recharge.paid_at = datetime.utcnow()

    user_result = await db.execute(select(User).where(User.id == recharge.user_id))
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
    current_user=Depends(get_current_admin_user),
):
    """取消充值订单"""
    result = await db.execute(select(Recharge).where(Recharge.id == recharge_id))
    recharge = result.scalar_one_or_none()
    if not recharge:
        raise HTTPException(status_code=404, detail="充值订单不存在")
    if recharge.status not in [RechargeStatus.PENDING]:
        raise HTTPException(status_code=400, detail="订单状态不允许取消")

    recharge.status = RechargeStatus.FAILED
    await db.commit()
    return {"message": "充值订单已取消"}


@router.put("/consumption/{order_id}/refund")
async def refund_consumption_order(
    order_id: UUID,
    reason: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """退款消费订单"""
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status != OrderStatus.PAID:
        raise HTTPException(status_code=400, detail="订单状态不允许退款")

    order.status = OrderStatus.REFUNDED
    user_result = await db.execute(select(User).where(User.id == order.user_id))
    user = user_result.scalar_one_or_none()
    if user:
        user.balance += abs(order.amount)

    await db.commit()
    return {"message": "订单已退款", "refund_amount": abs(order.amount)}


# ==================== 资源套餐 CRUD ====================

@router.get("/billing/plans", response_model=List[ResourcePlanResponse])
async def list_plans(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """管理端：获取所有资源套餐"""
    result = await db.execute(select(ResourcePlan).order_by(ResourcePlan.sort_order))
    return result.scalars().all()


@router.post("/billing/plans", response_model=ResourcePlanResponse)
async def create_plan(
    data: ResourcePlanCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """创建资源套餐"""
    plan = ResourcePlan(**data.model_dump())
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.put("/billing/plans/{plan_id}", response_model=ResourcePlanResponse)
async def update_plan(
    plan_id: UUID,
    data: ResourcePlanUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """更新资源套餐"""
    result = await db.execute(select(ResourcePlan).where(ResourcePlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="套餐不存在")

    update_data = data.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        setattr(plan, k, v)
    plan.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(plan)
    return plan


@router.delete("/billing/plans/{plan_id}")
async def delete_plan(
    plan_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """删除资源套餐"""
    result = await db.execute(select(ResourcePlan).where(ResourcePlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="套餐不存在")

    await db.delete(plan)
    await db.commit()
    return {"message": "套餐已删除"}
