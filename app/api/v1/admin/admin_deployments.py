"""管理后台 - 部署管理 API"""
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
logger = logging.getLogger("lmaicloud.admin_deployments")


class ScaleRequest(BaseModel):
    replicas: int


@router.get("", summary="获取 Deployment 列表")
async def list_deployments(
    namespace: Optional[str] = Query(None, description="命名空间，为空则查全部"),
    search: Optional[str] = Query(None, description="名称搜索"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}

    all_ns = namespace is None or namespace == ""
    deps = k8s.list_deployments(
        namespace=namespace or "default",
        all_namespaces=all_ns,
    )

    if search:
        deps = [d for d in deps if search.lower() in d["name"].lower()]

    total = len(deps)
    # 前端做客户端分页，后端返回全量数据
    return {"list": deps, "total": total}


@router.get("/{ns}/{name}", summary="获取 Deployment 详情")
async def get_deployment(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    dep = k8s.get_deployment(name, ns)
    if not dep:
        raise HTTPException(status_code=404, detail=f"Deployment {ns}/{name} 不存在")
    return dep


@router.post("", summary="创建 Deployment")
async def create_deployment(
    body: dict,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    namespace = body.get("metadata", {}).get("namespace", "default")
    name = body.get("metadata", {}).get("name", "unknown")
    result = k8s.create_deployment(namespace, body)
    if not result:
        raise HTTPException(status_code=400, detail="创建 Deployment 失败")
    await create_audit_log(
        db, current_user.id, AuditAction.CREATE, AuditResourceType.INSTANCE,
        resource_id=f"{namespace}/{name}", resource_name=name,
        detail=f"管理端创建 Deployment {namespace}/{name}",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": "创建成功", "name": result}


@router.put("/{ns}/{name}", summary="更新 Deployment")
async def update_deployment(
    ns: str, name: str,
    body: dict,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    ok = k8s.update_deployment(name, ns, body)
    if not ok:
        raise HTTPException(status_code=400, detail="更新 Deployment 失败")
    return {"message": "更新成功"}


@router.put("/{ns}/{name}/scale", summary="扩缩容 Deployment")
async def scale_deployment(
    ns: str, name: str,
    req: ScaleRequest,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    ok = k8s.scale_deployment(name, ns, req.replicas)
    if not ok:
        raise HTTPException(status_code=400, detail="扩缩容失败")
    await create_audit_log(
        db, current_user.id, AuditAction.UPDATE, AuditResourceType.INSTANCE,
        resource_id=f"{ns}/{name}", resource_name=name,
        detail=f"管理端扩缩容 Deployment {ns}/{name} -> {req.replicas} 副本",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": f"副本数已调整为 {req.replicas}"}


@router.post("/{ns}/{name}/restart", summary="滚动重启 Deployment")
async def restart_deployment(
    ns: str, name: str,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    ok = k8s.restart_deployment(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="重启失败")
    await create_audit_log(
        db, current_user.id, AuditAction.RESTART, AuditResourceType.INSTANCE,
        resource_id=f"{ns}/{name}", resource_name=name,
        detail=f"管理端重启 Deployment {ns}/{name}",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": "滚动重启已触发"}


@router.delete("/{ns}/{name}", summary="删除 Deployment")
async def delete_deployment(
    ns: str, name: str,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    ok = k8s.delete_deployment(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 Deployment 失败")
    await create_audit_log(
        db, current_user.id, AuditAction.DELETE, AuditResourceType.INSTANCE,
        resource_id=f"{ns}/{name}", resource_name=name,
        detail=f"管理端删除 Deployment {ns}/{name}",
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": "删除成功"}
