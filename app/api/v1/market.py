"""
市场 API - 公开的机器列表

所有节点数据从 K8s API 实时获取，不查询 DB nodes 表。
"""
from fastapi import APIRouter, Query, Depends
from typing import Optional
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.schemas import PaginatedResponse, MarketProductResponse
from app.models import MarketProduct
from app.services.k8s_client import get_k8s_client

router = APIRouter()


def _is_edge_node(labels: dict) -> bool:
    if labels.get("node-role.kubernetes.io/edge") is not None:
        return True
    if labels.get("node-role.kubernetes.io/agent") is not None:
        return True
    if labels.get("node-type") == "edge":
        return True
    return False


def _parse_cpu(raw) -> int:
    s = str(raw or "0")
    if s.endswith("m"):
        return int(s[:-1]) // 1000
    return int(s) if s.isdigit() else 2


def _parse_memory_gb(raw) -> int:
    s = str(raw or "0")
    if s.endswith("Ki"):
        return int(s[:-2]) // (1024 * 1024)
    if s.endswith("Mi"):
        return int(s[:-2]) // 1024
    if s.endswith("Gi"):
        return int(s[:-2])
    return 4


class MachineResponse(BaseModel):
    """机器响应模型"""
    id: str
    node_id: str
    name: str
    region: str
    gpu_model: str
    gpu_memory: str
    gpu_available: int
    gpu_total: int
    cpu_cores: int
    cpu_model: str
    memory: int
    disk: int
    gpu_driver: str
    cuda_version: str
    hourly_price: float
    member_price: float
    available_until: str
    node_type: str = "center"
    tag: Optional[str] = None


@router.get("/machines", response_model=PaginatedResponse)
async def list_machines(
    page: int = 1,
    size: int = 20,
    region: Optional[str] = None,
    gpu_model: Optional[str] = None,
    gpu_count: Optional[int] = None,
    billing_type: Optional[str] = None,
):
    """获取可租用机器列表 - 从 K8s 实时获取"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return PaginatedResponse(list=[], total=0, page=page, size=size)

    k8s_nodes = k8s.list_nodes()
    machines = []

    for kn in k8s_nodes:
        k8s_status = kn.get("status", "NotReady")
        if k8s_status != "Ready" or kn.get("unschedulable"):
            continue

        labels = kn.get("labels", {})
        kn_name = kn.get("name", "")
        kn_gpu_model = labels.get("nvidia.com/gpu.product", "N/A")
        kn_gpu_count = kn.get("gpu_count", 0)
        kn_gpu_alloc = kn.get("gpu_allocatable", 0)
        cpu_cores = _parse_cpu(kn.get("cpu_capacity"))
        mem_gb = _parse_memory_gb(kn.get("memory_capacity"))
        is_edge = _is_edge_node(labels)
        kn_type = "edge" if is_edge else "center"

        # 筛选: 有 GPU 的节点
        if gpu_count and kn_gpu_alloc < gpu_count:
            continue
        if not gpu_count and kn_gpu_alloc <= 0:
            continue

        # GPU 型号筛选
        if gpu_model and kn_gpu_model != gpu_model:
            continue

        hourly_price = 1.0 if kn_gpu_alloc > 0 else 0.1
        machines.append(MachineResponse(
            id=f"{kn_name}机",
            node_id=kn_name,
            name=kn_name,
            region=labels.get("topology.kubernetes.io/region", "默认区域"),
            gpu_model=kn_gpu_model,
            gpu_memory=f"{labels.get('nvidia.com/gpu.memory', '0')} MB",
            gpu_available=kn_gpu_alloc,
            gpu_total=kn_gpu_count,
            cpu_cores=cpu_cores,
            cpu_model=f"{cpu_cores}核",
            memory=mem_gb,
            disk=100,
            gpu_driver=labels.get("nvidia.com/driver.version", "--"),
            cuda_version=labels.get("nvidia.com/cuda.runtime.major", "--"),
            hourly_price=hourly_price,
            member_price=round(hourly_price * 0.95, 2),
            available_until="长期可用",
            node_type=kn_type,
        ))

    # 排序 + 分页
    machines.sort(key=lambda m: m.hourly_price)
    total = len(machines)
    start = (page - 1) * size
    paged = machines[start:start + size]

    return PaginatedResponse(list=paged, total=total, page=page, size=size)


@router.get("/regions")
async def list_regions():
    """获取可用区域列表 - 从 K8s labels 读取"""
    k8s = get_k8s_client()
    regions_set = set()

    if k8s.is_connected:
        for kn in k8s.list_nodes():
            labels = kn.get("labels", {})
            region = labels.get("topology.kubernetes.io/region")
            if region:
                regions_set.add(region)

    # 默认区域列表
    default_regions = [
        {"id": "beijing-b", "name": "北京B区"},
        {"id": "beijing-a", "name": "北京A区"},
        {"id": "northwest-b", "name": "西北B区"},
        {"id": "chongqing-a", "name": "重庆A区"},
    ]

    return {"regions": default_regions}


@router.get("/gpu-models")
async def list_gpu_models():
    """获取可用GPU型号列表 - 从 K8s 实时获取"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"gpu_models": []}

    model_stats = {}
    for kn in k8s.list_nodes():
        k8s_status = kn.get("status", "NotReady")
        if k8s_status != "Ready" or kn.get("unschedulable"):
            continue
        labels = kn.get("labels", {})
        model = labels.get("nvidia.com/gpu.product")
        if not model:
            continue
        gpu_count = kn.get("gpu_count", 0)
        gpu_alloc = kn.get("gpu_allocatable", 0)
        if model not in model_stats:
            model_stats[model] = {"available": 0, "total": 0}
        model_stats[model]["available"] += gpu_alloc
        model_stats[model]["total"] += gpu_count

    gpu_models = [
        {"id": model, "name": model, "available": s["available"], "total": s["total"]}
        for model, s in model_stats.items()
    ]
    gpu_models.sort(key=lambda x: x["available"], reverse=True)

    return {"gpu_models": gpu_models}


@router.get("/products")
async def list_market_products(
    category: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """获取上架的市场产品列表（公开接口）"""
    q = select(MarketProduct).where(MarketProduct.is_active == True)
    if category:
        q = q.where(MarketProduct.category == category)
    q = q.order_by(MarketProduct.sort_order)
    result = await db.execute(q)
    products = result.scalars().all()
    return [MarketProductResponse.model_validate(p).model_dump() for p in products]
