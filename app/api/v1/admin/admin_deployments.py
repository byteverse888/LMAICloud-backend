"""管理后台 - 部署管理 API"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from pydantic import BaseModel

from app.utils.auth import get_current_admin_user
from app.services.k8s_client import get_k8s_client

router = APIRouter()


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
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    namespace = body.get("metadata", {}).get("namespace", "default")
    result = k8s.create_deployment(namespace, body)
    if not result:
        raise HTTPException(status_code=400, detail="创建 Deployment 失败")
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
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    ok = k8s.scale_deployment(name, ns, req.replicas)
    if not ok:
        raise HTTPException(status_code=400, detail="扩缩容失败")
    return {"message": f"副本数已调整为 {req.replicas}"}


@router.post("/{ns}/{name}/restart", summary="滚动重启 Deployment")
async def restart_deployment(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    ok = k8s.restart_deployment(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="重启失败")
    return {"message": "滚动重启已触发"}


@router.delete("/{ns}/{name}", summary="删除 Deployment")
async def delete_deployment(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    ok = k8s.delete_deployment(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 Deployment 失败")
    return {"message": "删除成功"}
