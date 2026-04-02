"""管理端仪表盘 API"""
import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.utils.auth import get_current_admin_user
from app.models import User as AIUser, Instance, InstanceStatus, Order, OrderType, OrderStatus, Recharge, RechargeStatus, AuditLog
from app.services.k8s_client import get_k8s_client

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/stats")
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取仪表盘统计数据"""
    k8s = get_k8s_client()
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # 用户数
    user_count = (await db.execute(select(func.count(AIUser.id)))).scalar() or 0

    # 运行实例数
    running_instances = (await db.execute(
        select(func.count(Instance.id)).where(Instance.status == InstanceStatus.RUNNING)
    )).scalar() or 0

    total_instances = (await db.execute(select(func.count(Instance.id)))).scalar() or 0

    # K8s 节点和GPU
    nodes = k8s.list_nodes() if k8s.is_connected else []
    total_nodes = len(nodes)
    gpu_total = sum(n.get('gpu_count', 0) for n in nodes)
    gpu_used = gpu_total - sum(n.get('gpu_allocatable', 0) for n in nodes)

    # 今日收入(充值)
    today_income = (await db.execute(
        select(func.coalesce(func.sum(Recharge.amount), 0))
        .where(Recharge.status == RechargeStatus.SUCCESS, Recharge.created_at >= today_start)
    )).scalar() or 0

    # 本月收入
    month_income = (await db.execute(
        select(func.coalesce(func.sum(Recharge.amount), 0))
        .where(Recharge.status == RechargeStatus.SUCCESS, Recharge.created_at >= month_start)
    )).scalar() or 0

    # 今日消费
    today_expense = (await db.execute(
        select(func.coalesce(func.sum(Order.amount), 0))
        .where(Order.status == OrderStatus.PAID, Order.type != OrderType.RECHARGE, Order.created_at >= today_start)
    )).scalar() or 0

    # 今日新增用户
    today_new_users = (await db.execute(
        select(func.count(AIUser.id)).where(AIUser.created_at >= today_start)
    )).scalar() or 0

    # 最近活动(最新10条操作日志)
    recent_logs = (await db.execute(
        select(AuditLog, AIUser.email)
        .join(AIUser, AuditLog.user_id == AIUser.id)
        .order_by(AuditLog.created_at.desc())
        .limit(10)
    )).all()

    activities = [
        {
            "time": log.created_at.strftime("%H:%M") if log.created_at else "",
            "event": f"用户 {email} {log.action.value if log.action else ''} {log.resource_name or ''}",
            "type": "info",
        }
        for log, email in recent_logs
    ]

    return {
        "clusters": 1,
        "nodes": total_nodes,
        "users": user_count,
        "instances": total_instances,
        "running_instances": running_instances,
        "gpu_total": gpu_total,
        "gpu_used": gpu_used,
        "today_revenue": float(today_income),
        "month_revenue": float(month_income),
        "today_expense": float(today_expense),
        "today_new_users": today_new_users,
        "activities": activities,
    }
