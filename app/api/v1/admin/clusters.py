"""集群管理 API - 直接从K8s获取运行态数据"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.database import get_db
from app.config import settings
from app.services.k8s_client import get_k8s_client
from app.utils.auth import get_current_admin_user
from app.logging_config import get_logger

router = APIRouter()
logger = get_logger("lmaicloud.admin.clusters")


@router.get("/")
async def list_clusters(
    current_user = Depends(get_current_admin_user),
):
    """获取集群列表 - 从K8s实时获取"""
    k8s = get_k8s_client()
    
    # 获取K8s节点列表统计
    nodes = k8s.list_nodes() if k8s.is_connected else []
    
    gpu_total = sum(n.get('gpu_count', 0) for n in nodes)
    gpu_available = sum(n.get('gpu_allocatable', 0) for n in nodes)
    online_nodes = sum(1 for n in nodes if n.get('status') == 'Ready' and not n.get('unschedulable'))
    
    # 单集群架构，返回配置的集群信息
    cluster = {
        "id": "default",
        "name": settings.k8s_cluster_name,
        "region": settings.k8s_cluster_region,
        "status": "online" if k8s.is_connected and online_nodes > 0 else "offline",
        "nodes": len(nodes),
        "gpuTotal": gpu_total,
        "gpuAvailable": gpu_available,
        "description": f"K8s集群 - {len(nodes)}节点",
    }
    
    logger.info(f"获取集群列表 - 节点数: {len(nodes)}, GPU: {gpu_available}/{gpu_total}")
    return {"list": [cluster], "total": 1}


@router.get("/stats")
async def get_cluster_stats(
    current_user = Depends(get_current_admin_user),
):
    """获取集群统计信息"""
    k8s = get_k8s_client()
    nodes = k8s.list_nodes() if k8s.is_connected else []
    
    online_nodes = sum(1 for n in nodes if n.get('status') == 'Ready' and not n.get('unschedulable'))
    
    return {
        "total_clusters": 1,
        "online_clusters": 1 if k8s.is_connected and online_nodes > 0 else 0,
        "offline_clusters": 0 if k8s.is_connected and online_nodes > 0 else 1,
    }


@router.get("/{cluster_id}")
async def get_cluster(
    cluster_id: str,
    current_user = Depends(get_current_admin_user),
):
    """获取集群详情"""
    if cluster_id != "default":
        raise HTTPException(status_code=404, detail="集群不存在")
    
    k8s = get_k8s_client()
    nodes = k8s.list_nodes() if k8s.is_connected else []
    
    gpu_total = sum(n.get('gpu_count', 0) for n in nodes)
    gpu_available = sum(n.get('gpu_allocatable', 0) for n in nodes)
    online_nodes = sum(1 for n in nodes if n.get('status') == 'Ready' and not n.get('unschedulable'))
    
    return {
        "id": "default",
        "name": settings.k8s_cluster_name,
        "region": settings.k8s_cluster_region,
        "status": "online" if k8s.is_connected and online_nodes > 0 else "offline",
        "nodes": len(nodes),
        "gpuTotal": gpu_total,
        "gpuAvailable": gpu_available,
        "description": f"K8s集群 - {len(nodes)}节点",
    }
