"""数据报表 API (管理端)"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from typing import Optional
from datetime import datetime, timedelta

from app.database import get_db
from app.models import User, Instance, Order, Recharge, RechargeStatus, OrderStatus
from app.utils.auth import get_current_admin_user
from app.services.k8s_client import get_k8s_client

router = APIRouter()


@router.get("/stats")
async def get_report_stats(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取报表统计数据（前端卡片展示用）"""
    # 用户统计
    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0

    # 运行实例
    running_instances = (await db.execute(
        select(func.count(Instance.id)).where(Instance.status == "running")
    )).scalar() or 0

    # 财务统计
    total_revenue = (await db.execute(
        select(func.sum(Recharge.amount)).where(Recharge.status == RechargeStatus.SUCCESS)
    )).scalar() or 0

    # 今日新增
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_new_users = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= today_start)
    )).scalar() or 0
    today_orders = (await db.execute(
        select(func.count(Order.id)).where(Order.created_at >= today_start)
    )).scalar() or 0

    # GPU 使用率
    gpu_utilization = 0.0
    try:
        k8s = get_k8s_client()
        if k8s.is_connected:
            k8s_nodes = k8s.list_nodes()
            total_gpu = 0
            avail_gpu = 0
            for kn in k8s_nodes:
                if kn.get("status") == "Ready" and not kn.get("unschedulable"):
                    total_gpu += kn.get("gpu_count", 0)
                    avail_gpu += kn.get("gpu_allocatable", 0)
            if total_gpu > 0:
                gpu_utilization = round((total_gpu - avail_gpu) / total_gpu * 100, 1)
    except Exception:
        pass

    return {
        "totalUsers": total_users,
        "activeInstances": running_instances,
        "totalRevenue": float(total_revenue),
        "todayNewUsers": today_new_users,
        "todayOrders": today_orders,
        "gpuUtilization": gpu_utilization,
    }

@router.get("/overview")
async def get_overview_stats(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取总览统计数据"""
    # 用户统计
    total_users = await db.execute(select(func.count(User.id)))
    active_users = await db.execute(
        select(func.count(User.id)).where(User.status == "active")
    )
    
    # 实例统计
    total_instances = await db.execute(select(func.count(Instance.id)))
    running_instances = await db.execute(
        select(func.count(Instance.id)).where(Instance.status == "running")
    )
    
    # 节点统计 - 从 K8s 实时获取
    k8s = get_k8s_client()
    k8s_nodes = k8s.list_nodes() if k8s.is_connected else []
    total_nodes_count = len(k8s_nodes)
    online_nodes_count = sum(1 for n in k8s_nodes if n.get("status") == "Ready" and not n.get("unschedulable"))
    
    # 财务统计
    total_revenue = await db.execute(
        select(func.sum(Recharge.amount)).where(Recharge.status == RechargeStatus.SUCCESS)
    )
    total_consumption = await db.execute(
        select(func.sum(Order.amount)).where(Order.status == OrderStatus.PAID)
    )
    
    return {
        "users": {
            "total": total_users.scalar(),
            "active": active_users.scalar(),
        },
        "instances": {
            "total": total_instances.scalar(),
            "running": running_instances.scalar(),
        },
        "nodes": {
            "total": total_nodes_count,
            "online": online_nodes_count,
        },
        "finance": {
            "total_revenue": total_revenue.scalar() or 0,
            "total_consumption": total_consumption.scalar() or 0,
        },
    }


@router.get("/users/trend")
async def get_user_trend(
    days: int = Query(30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取用户增长趋势"""
    start_date = datetime.utcnow() - timedelta(days=days)
    
    result = await db.execute(
        select(
            func.date(User.created_at).label("date"),
            func.count(User.id).label("count"),
        )
        .where(User.created_at >= start_date)
        .group_by(func.date(User.created_at))
        .order_by(func.date(User.created_at))
    )
    
    trend_data = [{"date": str(row.date), "count": row.count} for row in result]
    return {"period_days": days, "data": trend_data}


@router.get("/revenue/trend")
async def get_revenue_trend(
    days: int = Query(30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取收入趋势"""
    start_date = datetime.utcnow() - timedelta(days=days)
    
    recharge_result = await db.execute(
        select(
            func.date(Recharge.created_at).label("date"),
            func.sum(Recharge.amount).label("amount"),
        )
        .where(
            Recharge.status == RechargeStatus.SUCCESS,
            Recharge.created_at >= start_date,
        )
        .group_by(func.date(Recharge.created_at))
        .order_by(func.date(Recharge.created_at))
    )
    
    trend_data = [
        {"date": str(row.date), "amount": float(row.amount or 0)} 
        for row in recharge_result
    ]
    return {"period_days": days, "data": trend_data}


@router.get("/consumption/trend")
async def get_consumption_trend(
    days: int = Query(30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取消费趋势"""
    start_date = datetime.utcnow() - timedelta(days=days)
    
    result = await db.execute(
        select(
            func.date(Order.created_at).label("date"),
            func.sum(Order.amount).label("amount"),
        )
        .where(
            Order.status == OrderStatus.PAID,
            Order.created_at >= start_date,
        )
        .group_by(func.date(Order.created_at))
        .order_by(func.date(Order.created_at))
    )
    
    trend_data = [
        {"date": str(row.date), "amount": float(row.amount or 0)} 
        for row in result
    ]
    return {"period_days": days, "data": trend_data}


@router.get("/instances/usage")
async def get_instance_usage(
    days: int = Query(30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取实例使用统计"""
    start_date = datetime.utcnow() - timedelta(days=days)
    
    result = await db.execute(
        select(
            func.date(Instance.created_at).label("date"),
            func.count(Instance.id).label("count"),
        )
        .where(Instance.created_at >= start_date)
        .group_by(func.date(Instance.created_at))
        .order_by(func.date(Instance.created_at))
    )
    
    trend_data = [{"date": str(row.date), "count": row.count} for row in result]
    return {"period_days": days, "data": trend_data}


@router.get("/gpu/usage")
async def get_gpu_usage(
    current_user = Depends(get_current_admin_user),
):
    """获取 GPU 使用情况 - 从 K8s 实时获取"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"total_gpu": 0, "available_gpu": 0, "used_gpu": 0, "utilization_rate": 0, "by_model": []}

    k8s_nodes = k8s.list_nodes()
    total = 0
    available = 0
    model_stats = {}

    for kn in k8s_nodes:
        if kn.get("status") != "Ready" or kn.get("unschedulable"):
            continue
        labels = kn.get("labels", {})
        gpu_model = labels.get("nvidia.com/gpu.product")
        gpu_count = kn.get("gpu_count", 0)
        gpu_alloc = kn.get("gpu_allocatable", 0)
        total += gpu_count
        available += gpu_alloc
        if gpu_model:
            if gpu_model not in model_stats:
                model_stats[gpu_model] = {"total": 0, "available": 0}
            model_stats[gpu_model]["total"] += gpu_count
            model_stats[gpu_model]["available"] += gpu_alloc

    used = total - available
    models = [
        {
            "model": m,
            "total": s["total"],
            "available": s["available"],
            "used": s["total"] - s["available"],
        }
        for m, s in model_stats.items()
    ]

    return {
        "total_gpu": total,
        "available_gpu": available,
        "used_gpu": used,
        "utilization_rate": round(used / total * 100, 2) if total > 0 else 0,
        "by_model": models,
    }


@router.get("/top/users")
async def get_top_users(
    limit: int = Query(10, ge=1, le=100),
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取消费 Top 用户"""
    start_date = datetime.utcnow() - timedelta(days=days)
    
    result = await db.execute(
        select(
            User.id,
            User.email,
            User.nickname,
            func.sum(Order.amount).label("total_consumption"),
        )
        .join(Order, Order.user_id == User.id)
        .where(
            Order.status == OrderStatus.PAID,
            Order.created_at >= start_date,
        )
        .group_by(User.id, User.email, User.nickname)
        .order_by(func.sum(Order.amount).desc())
        .limit(limit)
    )
    
    top_users = [
        {
            "user_id": row.id,
            "email": row.email,
            "nickname": row.nickname,
            "total_consumption": float(row.total_consumption or 0),
        }
        for row in result
    ]
    
    return {"period_days": days, "data": top_users}
