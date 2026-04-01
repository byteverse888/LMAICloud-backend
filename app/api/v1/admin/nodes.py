"""节点管理 API - 直接从K8s获取运行态数据"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional

from app.config import settings
from app.services.k8s_client import get_k8s_client
from app.utils.auth import get_current_admin_user
from app.logging_config import get_logger

router = APIRouter()
logger = get_logger("lmaicloud.admin.nodes")


def is_edge_node(node: dict) -> bool:
    """判断是否为边缘节点 - 通过labels判断"""
    labels = node.get('labels', {})
    # KubeEdge 边缘节点通常有这个label
    if labels.get('node-role.kubernetes.io/edge') is not None:
        return True
    # 或者通过 kubeedge 相关label判断
    if labels.get('node-role.kubernetes.io/agent') is not None:
        return True
    # 或者自定义的 node-type label
    if labels.get('node-type') == 'edge':
        return True
    return False


def _calc_cpu_percent(node_name: str, cpu_cores: int, metrics_map: dict) -> float:
    """计算CPU使用率"""
    m = metrics_map.get(node_name)
    if not m or cpu_cores <= 0:
        return 0.0
    return round(m.get('cpu_usage_millicores', 0) / (cpu_cores * 1000) * 100, 1)


def _calc_mem_percent(node_name: str, memory_gb: int, metrics_map: dict) -> float:
    """计算内存使用率"""
    m = metrics_map.get(node_name)
    if not m or memory_gb <= 0:
        return 0.0
    mem_used_gb = m.get('memory_usage_bytes', 0) / (1024 * 1024 * 1024)
    return round(mem_used_gb / memory_gb * 100, 1)


@router.get("/")
async def list_nodes(
    status: Optional[str] = None,
    node_type: Optional[str] = None,
    current_user = Depends(get_current_admin_user),
):
    """获取节点列表 - 从K8s实时获取"""
    k8s = get_k8s_client()
    
    if not k8s.is_connected:
        logger.warning("K8s客户端未连接")
        return {"list": [], "total": 0}
    
    k8s_nodes = k8s.list_nodes()
    
    # 获取节点资源使用率
    metrics_map = {}
    try:
        metrics = k8s.list_node_metrics()
        for m in metrics:
            metrics_map[m['name']] = m
    except Exception:
        pass
    
    node_list = []
    for node in k8s_nodes:
        # 解析状态
        k8s_status = node.get('status', 'NotReady')
        is_unschedulable = node.get('unschedulable', False)
        
        if k8s_status == 'Ready' and not is_unschedulable:
            node_status = 'online'
        elif is_unschedulable:
            node_status = 'busy'
        else:
            node_status = 'offline'
        
        # 状态过滤
        if status and node_status != status:
            continue
        
        # 判断节点类型
        is_edge = is_edge_node(node)
        node_type_value = 'edge' if is_edge else 'center'
        
        # 节点类型过滤
        if node_type and node_type_value != node_type:
            continue
        
        # 解析GPU型号
        labels = node.get('labels', {})
        gpu_model = labels.get('nvidia.com/gpu.product', 'Unknown GPU')
        
        # 解析CPU和内存
        cpu_capacity = node.get('cpu_capacity', '0')
        memory_capacity = node.get('memory_capacity', '0')
        
        # 转换CPU核数
        if cpu_capacity.endswith('m'):
            cpu_cores = int(cpu_capacity[:-1]) // 1000
        else:
            cpu_cores = int(cpu_capacity) if cpu_capacity.isdigit() else 0
        
        # 转换内存GB
        if memory_capacity.endswith('Ki'):
            memory_gb = int(memory_capacity[:-2]) // (1024 * 1024)
        elif memory_capacity.endswith('Mi'):
            memory_gb = int(memory_capacity[:-2]) // 1024
        elif memory_capacity.endswith('Gi'):
            memory_gb = int(memory_capacity[:-2])
        else:
            memory_gb = 0
        
        node_list.append({
            "id": node.get('name'),
            "name": node.get('name'),
            "cluster": settings.k8s_cluster_name,
            "status": node_status,
            "node_type": node_type_value,
            "gpu_model": gpu_model,
            "gpu_count": node.get('gpu_count', 0),
            "gpu_available": node.get('gpu_allocatable', 0),
            "cpu_cores": cpu_cores,
            "memory": memory_gb,
            "ip_address": node.get('ip'),
            "created_at": node.get('created_at'),
            "cpu_usage_percent": _calc_cpu_percent(node.get('name'), cpu_cores, metrics_map),
            "memory_usage_percent": _calc_mem_percent(node.get('name'), memory_gb, metrics_map),
            "gpu_usage_percent": round((node.get('gpu_count', 0) - node.get('gpu_allocatable', 0)) / max(node.get('gpu_count', 1), 1) * 100, 1) if node.get('gpu_count', 0) > 0 else 0,
        })
    
    logger.info(f"获取节点列表 - 总数: {len(node_list)}")
    return {"list": node_list, "total": len(node_list)}


@router.get("/stats")
async def get_node_stats(
    current_user = Depends(get_current_admin_user),
):
    """获取节点统计信息 - 从K8s实时获取"""
    k8s = get_k8s_client()
    
    if not k8s.is_connected:
        return {
            "total_nodes": 0,
            "online_nodes": 0,
            "offline_nodes": 0,
            "total_gpu": 0,
            "available_gpu": 0,
        }
    
    nodes = k8s.list_nodes()
    
    total_nodes = len(nodes)
    online_nodes = sum(1 for n in nodes if n.get('status') == 'Ready' and not n.get('unschedulable'))
    total_gpu = sum(n.get('gpu_count', 0) for n in nodes)
    available_gpu = sum(n.get('gpu_allocatable', 0) for n in nodes)
    
    return {
        "total_nodes": total_nodes,
        "online_nodes": online_nodes,
        "offline_nodes": total_nodes - online_nodes,
        "total_gpu": total_gpu,
        "available_gpu": available_gpu,
    }


@router.get("/{node_name}")
async def get_node(
    node_name: str,
    current_user = Depends(get_current_admin_user),
):
    """获取节点详情 - 从K8s实时获取"""
    k8s = get_k8s_client()
    
    if not k8s.is_connected:
        raise HTTPException(status_code=503, detail="K8s集群未连接")
    
    node = k8s.get_node(node_name)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    
    # 解析状态
    k8s_status = node.get('status', 'NotReady')
    is_unschedulable = node.get('unschedulable', False)
    
    if k8s_status == 'Ready' and not is_unschedulable:
        node_status = 'online'
    elif is_unschedulable:
        node_status = 'busy'
    else:
        node_status = 'offline'
    
    labels = node.get('labels', {})
    
    return {
        "id": node.get('name'),
        "name": node.get('name'),
        "cluster": settings.k8s_cluster_name,
        "status": node_status,
        "gpu_model": labels.get('nvidia.com/gpu.product', 'Unknown GPU'),
        "gpu_count": node.get('gpu_count', 0),
        "gpu_available": node.get('gpu_allocatable', 0),
        "ip_address": node.get('ip'),
        "os": node.get('os'),
        "kubelet_version": node.get('kubelet_version'),
        "conditions": node.get('conditions', {}),
        "created_at": node.get('created_at'),
    }


@router.put("/{node_name}/cordon")
async def cordon_node(
    node_name: str,
    current_user = Depends(get_current_admin_user),
):
    """设置节点不可调度（维护模式）"""
    k8s = get_k8s_client()
    
    if not k8s.is_connected:
        raise HTTPException(status_code=503, detail="K8s集群未连接")
    
    success = k8s.cordon_node(node_name)
    if not success:
        raise HTTPException(status_code=500, detail="操作失败")
    
    logger.info(f"节点 {node_name} 已设置为不可调度 - 管理员: {current_user.id}")
    return {"message": f"节点 {node_name} 已进入维护模式"}


@router.put("/{node_name}/uncordon")
async def uncordon_node(
    node_name: str,
    current_user = Depends(get_current_admin_user),
):
    """取消节点不可调度"""
    k8s = get_k8s_client()
    
    if not k8s.is_connected:
        raise HTTPException(status_code=503, detail="K8s集群未连接")
    
    success = k8s.uncordon_node(node_name)
    if not success:
        raise HTTPException(status_code=500, detail="操作失败")
    
    logger.info(f"节点 {node_name} 已恢复可调度 - 管理员: {current_user.id}")
    return {"message": f"节点 {node_name} 已恢复正常"}


@router.delete("/{node_name}")
async def delete_node(
    node_name: str,
    current_user = Depends(get_current_admin_user),
):
    """删除节点 - 从K8s集群中移除节点"""
    k8s = get_k8s_client()
    
    if not k8s.is_connected:
        raise HTTPException(status_code=503, detail="K8s集群未连接")
    
    # 检查节点是否存在
    node = k8s.get_node(node_name)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    
    # 先将节点设置为不可调度
    k8s.cordon_node(node_name)
    
    # 删除节点
    success = k8s.delete_node(node_name)
    if not success:
        raise HTTPException(status_code=500, detail="删除节点失败")
    
    logger.info(f"节点 {node_name} 已删除 - 管理员: {current_user.id}")
    return {"message": f"节点 {node_name} 已成功删除"}
