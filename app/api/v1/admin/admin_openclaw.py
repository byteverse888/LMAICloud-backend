"""
OpenClaw 管理端 API - 管理员查看 / 管理所有用户的 OpenClaw 实例
"""
import asyncio
import logging
from uuid import UUID
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import AIUser as User, OpenClawInstance
from app.utils.auth import get_current_admin_user

logger = logging.getLogger(__name__)
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


# ── 管理员获取单个实例详情 ──

async def _admin_get_instance(instance_id: UUID, db: AsyncSession) -> OpenClawInstance:
    """管理员查询单个实例（不限用户）"""
    result = await db.execute(
        select(OpenClawInstance)
        .options(selectinload(OpenClawInstance.user))
        .where(OpenClawInstance.id == instance_id)
    )
    inst = result.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")
    return inst


@router.get("/instances/{instance_id}", summary="管理员获取实例详情")
async def admin_get_instance_detail(
    instance_id: UUID,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员获取单个 OpenClaw 实例详情（含 Pod 运行时信息）"""
    inst = await _admin_get_instance(instance_id, db)
    d = jsonable_encoder(inst)
    d["user_email"] = inst.user.email if inst.user else None

    # 查询实时 Pod 信息
    try:
        from app.services.k8s_client import get_k8s_client
        k8s = get_k8s_client()
        if k8s.is_connected:
            pods = await asyncio.to_thread(
                k8s.list_pods,
                inst.namespace,
                f"openclaw-instance={inst.id}",
            )
            if pods:
                pod = pods[0]
                d["pod_name"] = pod.get("name")
                d["pod_status"] = pod.get("effective_status") or pod.get("status")
                d["pod_ip"] = pod.get("pod_ip")
                d["pod_node"] = pod.get("node_name")
                d["host_ip"] = pod.get("host_ip")
                d["restart_count"] = pod.get("restart_count", 0)
                d["container_statuses"] = pod.get("container_statuses", [])
                d["events"] = k8s.get_pod_events(pod["name"], inst.namespace)
    except Exception as e:
        logger.warning(f"获取 OpenClaw Pod 详情失败: {e}")

    return d


@router.get("/instances/{instance_id}/logs", summary="管理员获取实例日志")
async def admin_get_instance_logs(
    instance_id: UUID,
    tail: int = Query(200, ge=1, le=5000, description="尾部行数"),
    container: Optional[str] = Query(None, description="容器名称，默认 gateway"),
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员获取 OpenClaw 实例 Pod 日志"""
    inst = await _admin_get_instance(instance_id, db)

    from app.services.k8s_client import get_k8s_client
    k8s = get_k8s_client()

    # 通过标签查找 Pod
    try:
        pods = await asyncio.to_thread(
            k8s.list_pods,
            inst.namespace,
            f"openclaw-instance={inst.id}",
        )
    except Exception as e:
        logger.warning(f"管理端 OpenClaw 日志: list_pods 失败 ns={inst.namespace} id={inst.id}: {e}")
        raise HTTPException(status_code=500, detail=f"获取 Pod 列表失败: {e}")

    if not pods:
        raise HTTPException(status_code=404, detail=f"未找到运行中的 Pod (ns={inst.namespace}, label=openclaw-instance={inst.id})")

    pod_name = pods[0]["name"]
    # 默认容器为 gateway（OpenClaw Pod 含 init-config 初始化容器）
    target_container = container or "gateway"
    try:
        logs = await asyncio.to_thread(
            k8s.get_pod_logs, pod_name, inst.namespace, tail, target_container,
        )
    except Exception as e:
        logger.warning(f"管理端 OpenClaw 日志: get_pod_logs 失败 pod={pod_name}: {e}")
        raise HTTPException(status_code=500, detail=f"获取日志失败: {e}")

    if logs is None:
        pod_status = pods[0].get("effective_status", pods[0].get("status", "unknown"))
        raise HTTPException(status_code=404, detail=f"日志为空，容器可能尚未就绪 (status={pod_status})")
    return {"logs": logs}


@router.delete("/instances/{instance_id}", summary="管理员删除实例")
async def admin_delete_instance(
    instance_id: UUID,
    request: Request,
    admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """管理员删除 OpenClaw 实例（释放所有 K8s 资源）"""
    inst = await _admin_get_instance(instance_id, db)

    if inst.status == "released":
        raise HTTPException(status_code=400, detail="实例已释放")

    # 即时结算
    try:
        from app.api.v1.billing import settle_instance_billing
        user_result = await db.execute(select(User).where(User.id == inst.user_id))
        user_obj = user_result.scalar_one_or_none()
        if user_obj:
            await settle_instance_billing(inst, user_obj, db, "openclaw", "管理员删除结算")
    except Exception as e:
        logger.warning(f"OpenClaw 管理员删除结算失败: {e}")

    inst.status = "releasing"
    await db.commit()

    from app.services.openclaw_manager import get_openclaw_manager
    mgr = get_openclaw_manager()
    await asyncio.to_thread(
        mgr.release_instance,
        instance_id=str(inst.id),
        namespace=inst.namespace,
        node_type=inst.node_type or "center",
    )

    inst.status = "released"
    await db.commit()

    # 审计日志
    try:
        from app.api.v1.audit_log import create_audit_log
        from app.models import AuditAction, AuditResourceType
        from app.utils.auth import get_client_ip
        await create_audit_log(
            db, admin.id, AuditAction.DELETE, AuditResourceType.OPENCLAW,
            resource_id=str(inst.id), resource_name=inst.name,
            detail=f"管理员删除 OpenClaw 实例: {inst.name}",
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"记录管理员删除 OpenClaw 审计日志失败: {e}")

    return {"detail": "实例已释放"}
