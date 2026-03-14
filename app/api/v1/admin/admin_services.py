"""管理后台 - 服务管理 API"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from app.utils.auth import get_current_admin_user
from app.services.k8s_client import get_k8s_client

router = APIRouter()


@router.get("", summary="获取 Service 列表")
async def list_services(
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
    svcs = k8s.list_services(
        namespace=namespace or "default",
        all_namespaces=all_ns,
    )

    # 名称搜索
    if search:
        svcs = [s for s in svcs if search.lower() in s["name"].lower()]

    total = len(svcs)
    # 前端做客户端分页，后端返回全量数据
    return {"list": svcs, "total": total}


@router.get("/{ns}/{name}", summary="获取 Service 详情")
async def get_service(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    svc = k8s.get_service(name, ns)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service {ns}/{name} 不存在")
    return svc


@router.post("", summary="创建 Service")
async def create_service(
    body: dict,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    namespace = body.get("metadata", {}).get("namespace", "default")
    result = k8s.create_service(namespace, body)
    if not result:
        raise HTTPException(status_code=400, detail="创建 Service 失败")
    return {"message": "创建成功", "name": result}


@router.put("/{ns}/{name}", summary="更新 Service")
async def update_service(
    ns: str, name: str,
    body: dict,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    ok = k8s.update_service(name, ns, body)
    if not ok:
        raise HTTPException(status_code=400, detail="更新 Service 失败")
    return {"message": "更新成功"}


@router.delete("/{ns}/{name}", summary="删除 Service")
async def delete_service(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    ok = k8s.delete_service(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 Service 失败")
    return {"message": "删除成功"}
