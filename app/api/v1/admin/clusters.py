"""集群管理 API - 直接从K8s获取运行态数据"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel, Field

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


# ===== 受保护的系统命名空间 =====
_PROTECTED_NAMESPACES = {"default", "kube-system", "kube-public", "kube-node-lease"}


class NamespaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=63, pattern=r'^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$')
    labels: Optional[dict] = None


@router.get("/namespaces/list", summary="获取命名空间列表")
async def list_namespaces(
    search: Optional[str] = Query(None),
    current_user=Depends(get_current_admin_user),
):
    """获取 K8s 命名空间列表"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}
    try:
        ns_list = k8s.list_namespaces()
        if search:
            ns_list = [ns for ns in ns_list if search.lower() in ns["name"].lower()]
        return {"list": ns_list, "total": len(ns_list)}
    except Exception as e:
        logger.error(f"获取命名空间列表失败: {e}")
        return {"list": [], "total": 0}


@router.post("/namespaces", summary="创建命名空间")
async def create_namespace(
    body: NamespaceCreate,
    current_user=Depends(get_current_admin_user),
):
    """创建 K8s 命名空间"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        raise HTTPException(status_code=503, detail="K8s 集群未连接")
    ok = k8s.create_namespace(body.name, labels=body.labels)
    if not ok:
        raise HTTPException(status_code=400, detail=f"创建命名空间 {body.name} 失败，可能已存在")
    logger.info(f"创建命名空间 {body.name}")
    return {"message": "创建成功", "name": body.name}


@router.get("/namespaces/{name}", summary="获取命名空间详情")
async def get_namespace(
    name: str,
    current_user=Depends(get_current_admin_user),
):
    """获取命名空间详情及资源用量"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        raise HTTPException(status_code=503, detail="K8s 集群未连接")
    ns = k8s.get_namespace(name)
    if not ns:
        raise HTTPException(status_code=404, detail=f"命名空间 {name} 不存在")
    ns["resources"] = k8s.get_namespace_resource_quota(name)
    ns["protected"] = name in _PROTECTED_NAMESPACES
    return ns


@router.delete("/namespaces/{name}", summary="删除命名空间")
async def delete_namespace(
    name: str,
    current_user=Depends(get_current_admin_user),
):
    """删除 K8s 命名空间（保护系统命名空间）"""
    if name in _PROTECTED_NAMESPACES:
        raise HTTPException(status_code=403, detail=f"系统命名空间 {name} 不可删除")
    k8s = get_k8s_client()
    if not k8s.is_connected:
        raise HTTPException(status_code=503, detail="K8s 集群未连接")
    ok = k8s.delete_namespace(name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"删除命名空间 {name} 失败")
    logger.info(f"删除命名空间 {name}")
    return {"message": "删除成功"}


@router.get("/health", summary="集群健康检查")
async def get_cluster_health(
    current_user=Depends(get_current_admin_user),
):
    """获取集群健康状态（等价于 kubectl get --raw='/readyz?verbose'）"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"status": "unknown", "checks": []}
    try:
        result = k8s.get_cluster_health()
        return result
    except Exception as e:
        logger.error(f"获取集群健康状态失败: {e}")
        return {"status": "error", "checks": [], "error": str(e)}


@router.get("/node-metrics", summary="节点资源指标")
async def get_node_metrics(
    current_user=Depends(get_current_admin_user),
):
    """获取所有节点的资源使用指标（等价于 kubectl top node）"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": []}
    try:
        # 获取 metrics
        metrics = k8s.list_node_metrics()
        # 获取节点列表以计算 capacity 和百分比
        nodes = k8s.list_nodes()
        node_capacity = {}
        for n in nodes:
            name = n.get('name')
            cpu_cap = n.get('cpu_capacity', '0')
            mem_cap = n.get('memory_capacity', '0')
            # 解析 CPU capacity
            if cpu_cap.endswith('m'):
                cpu_cap_mc = int(cpu_cap[:-1])
            elif cpu_cap.isdigit():
                cpu_cap_mc = int(cpu_cap) * 1000
            else:
                cpu_cap_mc = 0
            # 解析 Memory capacity
            if mem_cap.endswith('Ki'):
                mem_cap_bytes = int(mem_cap[:-2]) * 1024
            elif mem_cap.endswith('Mi'):
                mem_cap_bytes = int(mem_cap[:-2]) * 1024 * 1024
            elif mem_cap.endswith('Gi'):
                mem_cap_bytes = int(mem_cap[:-2]) * 1024 * 1024 * 1024
            elif mem_cap.isdigit():
                mem_cap_bytes = int(mem_cap)
            else:
                mem_cap_bytes = 0
            node_capacity[name] = {
                "cpu_capacity_millicores": cpu_cap_mc,
                "memory_capacity_bytes": mem_cap_bytes,
                "status": n.get('status'),
                "gpu_count": n.get('gpu_count', 0),
                "gpu_allocatable": n.get('gpu_allocatable', 0),
            }

        # 合并 metrics + capacity
        result = []
        for m in metrics:
            name = m['name']
            cap = node_capacity.get(name, {})
            cpu_cap_mc = cap.get('cpu_capacity_millicores', 0)
            mem_cap_bytes = cap.get('memory_capacity_bytes', 0)
            cpu_usage_mc = m['cpu_usage_millicores']
            mem_usage_bytes = m['memory_usage_bytes']
            result.append({
                "name": name,
                "cpu_usage_millicores": cpu_usage_mc,
                "cpu_capacity_millicores": cpu_cap_mc,
                "cpu_percent": round(cpu_usage_mc / cpu_cap_mc * 100, 1) if cpu_cap_mc > 0 else 0,
                "memory_usage_bytes": mem_usage_bytes,
                "memory_capacity_bytes": mem_cap_bytes,
                "memory_percent": round(mem_usage_bytes / mem_cap_bytes * 100, 1) if mem_cap_bytes > 0 else 0,
                "status": cap.get('status', 'Unknown'),
                "gpu_count": cap.get('gpu_count', 0),
                "gpu_allocatable": cap.get('gpu_allocatable', 0),
            })
        return {"list": result}
    except Exception as e:
        logger.error(f"获取节点指标失败: {e}")
        return {"list": [], "error": str(e)}


@router.get("/overview", summary="集群总览")
async def get_cluster_overview(
    current_user=Depends(get_current_admin_user),
):
    """获取集群完整概览信息，包含版本、节点统计、资源汇总"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"connected": False}

    # 获取各维度数据
    nodes = k8s.list_nodes()
    version = k8s.get_cluster_version()
    namespaces = k8s.list_namespaces()
    
    # 统计节点
    total_nodes = len(nodes)
    ready_nodes = sum(1 for n in nodes if n.get('status') == 'Ready' and not n.get('unschedulable'))
    total_gpu = sum(n.get('gpu_count', 0) for n in nodes)
    available_gpu = sum(n.get('gpu_allocatable', 0) for n in nodes)
    
    # 统计CPU/内存 capacity
    total_cpu_mc = 0
    total_mem_bytes = 0
    for n in nodes:
        cpu_cap = n.get('cpu_capacity', '0')
        mem_cap = n.get('memory_capacity', '0')
        if cpu_cap.endswith('m'):
            total_cpu_mc += int(cpu_cap[:-1])
        elif cpu_cap.isdigit():
            total_cpu_mc += int(cpu_cap) * 1000
        if mem_cap.endswith('Ki'):
            total_mem_bytes += int(mem_cap[:-2]) * 1024
        elif mem_cap.endswith('Mi'):
            total_mem_bytes += int(mem_cap[:-2]) * 1024 * 1024
        elif mem_cap.endswith('Gi'):
            total_mem_bytes += int(mem_cap[:-2]) * 1024 * 1024 * 1024

    # 统计 Pod 数量
    try:
        pods = k8s.list_pods(namespace="", all_namespaces=True)
        total_pods = len(pods)
        running_pods = sum(1 for p in pods if p.get('status') == 'Running')
    except Exception:
        total_pods = 0
        running_pods = 0

    return {
        "connected": True,
        "cluster_name": settings.k8s_cluster_name,
        "region": settings.k8s_cluster_region,
        "version": version,
        "nodes": {
            "total": total_nodes,
            "ready": ready_nodes,
            "not_ready": total_nodes - ready_nodes,
        },
        "resources": {
            "cpu_capacity_cores": round(total_cpu_mc / 1000, 1),
            "memory_capacity_gb": round(total_mem_bytes / (1024 ** 3), 1),
            "gpu_total": total_gpu,
            "gpu_available": available_gpu,
        },
        "pods": {
            "total": total_pods,
            "running": running_pods,
        },
        "namespaces": len(namespaces),
    }


@router.get("/events", summary="集群告警事件")
async def get_cluster_events(
    limit: int = 50,
    current_user=Depends(get_current_admin_user),
):
    """获取集群 Warning 事件"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": []}
    try:
        events = k8s.list_warning_events(limit=limit)
        return {"list": events}
    except Exception as e:
        logger.error(f"获取集群事件失败: {e}")
        return {"list": [], "error": str(e)}


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
