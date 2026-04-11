"""管理后台 - ConfigMap / Secret 管理 API"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from app.utils.auth import get_current_admin_user
from app.services.k8s_client import get_k8s_client

router = APIRouter()


# ========== ConfigMap ==========

@router.get("/configmaps", summary="获取 ConfigMap 列表")
async def list_config_maps(
    namespace: Optional[str] = Query(None, description="命名空间，为空则查全部"),
    search: Optional[str] = Query(None, description="名称搜索"),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}
    try:
        all_ns = namespace is None or namespace == ""
        items = k8s.list_config_maps(
            namespace=namespace or "default",
            all_namespaces=all_ns,
        )
    except Exception:
        items = []
    if search:
        items = [i for i in items if search.lower() in i["name"].lower()]
    return {"list": items, "total": len(items)}


@router.get("/configmaps/{ns}/{name}", summary="获取 ConfigMap 详情")
async def get_config_map(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    cm = k8s.get_config_map(name, ns)
    if not cm:
        raise HTTPException(status_code=404, detail=f"ConfigMap {ns}/{name} 不存在")
    return cm


@router.delete("/configmaps/{ns}/{name}", summary="删除 ConfigMap")
async def delete_config_map(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    ok = k8s.delete_config_map(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 ConfigMap 失败")
    return {"message": "删除成功"}


# ========== Secret ==========

@router.get("/secrets", summary="获取 Secret 列表")
async def list_secrets(
    namespace: Optional[str] = Query(None, description="命名空间，为空则查全部"),
    search: Optional[str] = Query(None, description="名称搜索"),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}
    try:
        all_ns = namespace is None or namespace == ""
        items = k8s.list_secrets(
            namespace=namespace or "default",
            all_namespaces=all_ns,
        )
    except Exception:
        items = []
    if search:
        items = [i for i in items if search.lower() in i["name"].lower()]
    return {"list": items, "total": len(items)}


@router.get("/secrets/{ns}/{name}", summary="获取 Secret 详情（值脱敏）")
async def get_secret(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    s = k8s.get_secret(name, ns)
    if not s:
        raise HTTPException(status_code=404, detail=f"Secret {ns}/{name} 不存在")
    return s


@router.delete("/secrets/{ns}/{name}", summary="删除 Secret")
async def delete_secret(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    ok = k8s.delete_secret(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 Secret 失败")
    return {"message": "删除成功"}
