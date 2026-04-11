"""管理后台 - 工作负载管理 API（DaemonSet / StatefulSet）"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.auth import get_current_admin_user
from app.services.k8s_client import get_k8s_client
from app.database import get_db
from app.api.v1.audit_log import create_audit_log, get_client_ip
from app.models import AuditAction, AuditResourceType

router = APIRouter()
logger = logging.getLogger("lmaicloud.admin_workloads")


class ScaleRequest(BaseModel):
    replicas: int


# ==================== DaemonSet ====================


@router.get("/daemonsets", summary="获取 DaemonSet 列表")
async def list_daemon_sets(
    namespace: Optional[str] = Query(None, description="命名空间，为空则查全部"),
    search: Optional[str] = Query(None, description="名称搜索"),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}

    all_ns = namespace is None or namespace == ""
    ds_list = k8s.list_daemon_sets(
        namespace=namespace or "default",
        all_namespaces=all_ns,
    )

    if search:
        ds_list = [d for d in ds_list if search.lower() in d["name"].lower()]

    return {"list": ds_list, "total": len(ds_list)}


@router.post("/daemonsets/{ns}/{name}/restart", summary="滚动重启 DaemonSet")
async def restart_daemon_set(
    ns: str, name: str,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    ok = k8s.restart_daemon_set(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="重启 DaemonSet 失败")
    await create_audit_log(
        db, current_user.id, AuditAction.RESTART, AuditResourceType.INSTANCE,
        resource_id=f"{ns}/{name}", resource_name=name,
        detail=f"管理端重启 DaemonSet {ns}/{name}",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": "滚动重启已触发"}


@router.delete("/daemonsets/{ns}/{name}", summary="删除 DaemonSet")
async def delete_daemon_set(
    ns: str, name: str,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    ok = k8s.delete_daemon_set(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 DaemonSet 失败")
    await create_audit_log(
        db, current_user.id, AuditAction.DELETE, AuditResourceType.INSTANCE,
        resource_id=f"{ns}/{name}", resource_name=name,
        detail=f"管理端删除 DaemonSet {ns}/{name}",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": "删除成功"}


# ==================== StatefulSet ====================


@router.get("/statefulsets", summary="获取 StatefulSet 列表")
async def list_stateful_sets(
    namespace: Optional[str] = Query(None, description="命名空间，为空则查全部"),
    search: Optional[str] = Query(None, description="名称搜索"),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}

    all_ns = namespace is None or namespace == ""
    ss_list = k8s.list_stateful_sets(
        namespace=namespace or "default",
        all_namespaces=all_ns,
    )

    if search:
        ss_list = [s for s in ss_list if search.lower() in s["name"].lower()]

    return {"list": ss_list, "total": len(ss_list)}


@router.put("/statefulsets/{ns}/{name}/scale", summary="扩缩容 StatefulSet")
async def scale_stateful_set(
    ns: str, name: str,
    req: ScaleRequest,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    ok = k8s.scale_stateful_set(name, ns, req.replicas)
    if not ok:
        raise HTTPException(status_code=400, detail="扩缩容失败")
    await create_audit_log(
        db, current_user.id, AuditAction.UPDATE, AuditResourceType.INSTANCE,
        resource_id=f"{ns}/{name}", resource_name=name,
        detail=f"管理端扩缩容 StatefulSet {ns}/{name} -> {req.replicas} 副本",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": f"副本数已调整为 {req.replicas}"}


@router.post("/statefulsets/{ns}/{name}/restart", summary="滚动重启 StatefulSet")
async def restart_stateful_set(
    ns: str, name: str,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    ok = k8s.restart_stateful_set(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="重启 StatefulSet 失败")
    await create_audit_log(
        db, current_user.id, AuditAction.RESTART, AuditResourceType.INSTANCE,
        resource_id=f"{ns}/{name}", resource_name=name,
        detail=f"管理端重启 StatefulSet {ns}/{name}",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": "滚动重启已触发"}


@router.delete("/statefulsets/{ns}/{name}", summary="删除 StatefulSet")
async def delete_stateful_set(
    ns: str, name: str,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    ok = k8s.delete_stateful_set(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 StatefulSet 失败")
    await create_audit_log(
        db, current_user.id, AuditAction.DELETE, AuditResourceType.INSTANCE,
        resource_id=f"{ns}/{name}", resource_name=name,
        detail=f"管理端删除 StatefulSet {ns}/{name}",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": "删除成功"}
