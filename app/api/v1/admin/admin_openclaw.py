"""
OpenClaw 管理端 API - 管理员查看所有用户的 OpenClaw 实例
"""
import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

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
    """管理员查看所有 OpenClaw 实例（含 K8s Pod 实时信息）"""
    query = (
        select(OpenClawInstance)
        .options(selectinload(OpenClawInstance.user))
        .where(OpenClawInstance.status != "released")
    )

    if status:
        query = query.where(OpenClawInstance.status == status)
    if search:
        query = query.where(OpenClawInstance.name.ilike(f"%{search}%"))

    query = query.order_by(OpenClawInstance.created_at.desc())
    result = await db.execute(query)
    instances = result.scalars().all()

    # ── 批量获取 K8s Pod 信息（单次 API 调用） ──
    pod_map: dict = {}
    try:
        from app.services.k8s_client import get_k8s_client
        k8s = get_k8s_client()
        if k8s.is_connected and not getattr(k8s, 'circuit_open', False):
            pods = await asyncio.to_thread(
                k8s.list_pods,
                label_selector="app=openclaw",
                all_namespaces=True,
            )
            for pod in (pods or []):
                labels = pod.get("labels") or {}
                iid = labels.get("openclaw-instance")
                if iid:
                    pod_map[iid] = {
                        "pod_name": pod.get("name"),
                        "pod_status": pod.get("effective_status") or pod.get("status"),
                        "pod_ip": pod.get("pod_ip"),
                        "pod_node": pod.get("node_name"),
                    }
    except Exception:
        pass

    # ── 构建响应：实例 + Pod 信息 + 用户邮箱 ──
    result_list = []
    for inst in instances:
        d = jsonable_encoder(inst)
        d["user_email"] = inst.user.email if inst.user else None
        pod = pod_map.get(str(inst.id))
        if pod:
            d["pod_status"] = pod["pod_status"]
            d["pod_ip"] = pod["pod_ip"]
            d["pod_node"] = pod["pod_node"]
        else:
            d["pod_status"] = None
            d["pod_ip"] = None
            d["pod_node"] = None
        result_list.append(d)

    return {"list": result_list, "total": len(result_list)}
