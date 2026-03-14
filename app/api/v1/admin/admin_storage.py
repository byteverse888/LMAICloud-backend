"""管理后台 - 存储管理 API (PV / PVC / StorageClass)"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from app.utils.auth import get_current_admin_user
from app.services.k8s_client import get_k8s_client

router = APIRouter()


# ========== PersistentVolume ==========

@router.get("/pvs", summary="获取 PV 列表")
async def list_pvs(
    search: Optional[str] = Query(None),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}
    try:
        pvs = k8s.list_pvs()
    except Exception:
        pvs = []
    if search:
        pvs = [p for p in pvs if search.lower() in p["name"].lower()]
    return {"list": pvs, "total": len(pvs)}


@router.get("/pvs/{name}", summary="获取 PV 详情")
async def get_pv(name: str, current_user=Depends(get_current_admin_user)):
    k8s = get_k8s_client()
    pv = k8s.get_pv(name)
    if not pv:
        raise HTTPException(status_code=404, detail=f"PV {name} 不存在")
    return pv


@router.delete("/pvs/{name}", summary="删除 PV")
async def delete_pv(name: str, current_user=Depends(get_current_admin_user)):
    k8s = get_k8s_client()
    ok = k8s.delete_pv(name)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 PV 失败")
    return {"message": "删除成功"}


# ========== PersistentVolumeClaim ==========

@router.get("/pvcs", summary="获取 PVC 列表")
async def list_pvcs(
    namespace: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}
    try:
        all_ns = namespace is None or namespace == ""
        pvcs = k8s.list_pvcs(namespace=namespace or "default", all_namespaces=all_ns)
    except Exception:
        pvcs = []
    if search:
        pvcs = [p for p in pvcs if search.lower() in p["name"].lower()]
    return {"list": pvcs, "total": len(pvcs)}


@router.get("/pvcs/{ns}/{name}", summary="获取 PVC 详情")
async def get_pvc(ns: str, name: str, current_user=Depends(get_current_admin_user)):
    k8s = get_k8s_client()
    pvc = k8s.get_pvc(name, ns)
    if not pvc:
        raise HTTPException(status_code=404, detail=f"PVC {ns}/{name} 不存在")
    return pvc


@router.delete("/pvcs/{ns}/{name}", summary="删除 PVC")
async def delete_pvc(ns: str, name: str, current_user=Depends(get_current_admin_user)):
    k8s = get_k8s_client()
    ok = k8s.delete_pvc(name, ns)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 PVC 失败")
    return {"message": "删除成功"}


# ========== StorageClass ==========

@router.get("/storageclasses", summary="获取 StorageClass 列表")
async def list_storage_classes(
    search: Optional[str] = Query(None),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}
    try:
        scs = k8s.list_storage_classes()
    except Exception:
        scs = []
    if search:
        scs = [s for s in scs if search.lower() in s["name"].lower()]
    return {"list": scs, "total": len(scs)}


@router.get("/storageclasses/{name}", summary="获取 StorageClass 详情")
async def get_storage_class(name: str, current_user=Depends(get_current_admin_user)):
    k8s = get_k8s_client()
    sc = k8s.get_storage_class(name)
    if not sc:
        raise HTTPException(status_code=404, detail=f"StorageClass {name} 不存在")
    return sc
