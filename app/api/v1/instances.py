"""
容器实例 API

节点/Pod/Deployment/Service 等 K8s 资源全部直接和 K8s API 交互，
不再查询 DB 的 nodes 表。Instance 记录仍写入 DB 用于计费和用户管理。
"""
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db, AsyncSessionLocal
from app.models import User, Instance, AppImage
from app.schemas import (
    InstanceCreate, InstanceResponse, ResourceConfigResponse, PaginatedResponse,
)
from app.utils.auth import get_current_user
from app.services.k8s_client import get_k8s_client
from app.services.pod_manager import get_pod_manager
from app.services.ws_manager import broadcast_instance_status
from app.logging_config import get_logger

router = APIRouter()
logger = get_logger("lmaicloud.instances")


# ========== 辅助函数 ==========

def _is_edge_node(labels: dict) -> bool:
    """通过 K8s labels 判断是否为边缘节点"""
    if labels.get("node-role.kubernetes.io/edge") is not None:
        return True
    if labels.get("node-role.kubernetes.io/agent") is not None:
        return True
    if labels.get("node-type") == "edge":
        return True
    return False


def _parse_cpu(raw: str) -> int:
    if not raw:
        return 2
    if str(raw).endswith("m"):
        return int(raw[:-1]) // 1000
    return int(raw) if str(raw).isdigit() else 2


def _parse_memory_gb(raw: str) -> int:
    if not raw:
        return 4
    s = str(raw)
    if s.endswith("Ki"):
        return int(s[:-2]) // (1024 * 1024)
    if s.endswith("Mi"):
        return int(s[:-2]) // 1024
    if s.endswith("Gi"):
        return int(s[:-2])
    return 4


# ========== 资源配置 ==========

@router.get("/resource-configs", summary="获取可用资源配置列表")
async def list_resource_configs(
    gpu_model: Optional[str] = None,
    node_type: Optional[str] = None,
    resource_type: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """
    从 K8s API 实时读取节点信息，返回可租用的资源配置列表。
    """
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}

    k8s_nodes = k8s.list_nodes()
    configs = []

    for kn in k8s_nodes:
        kn_name = kn.get("name", "")
        k8s_status = kn.get("status", "NotReady")
        if k8s_status != "Ready" or kn.get("unschedulable"):
            continue

        labels = kn.get("labels", {})
        is_edge = _is_edge_node(labels)
        kn_type = "edge" if is_edge else "center"

        # 节点类型过滤
        if node_type and kn_type != node_type:
            continue

        cpu_cores = _parse_cpu(kn.get("cpu_capacity", "0"))
        mem_gb = _parse_memory_gb(kn.get("memory_capacity", "0"))
        kn_gpu_model = labels.get("nvidia.com/gpu.product", "N/A")
        kn_gpu_count = kn.get("gpu_count", 0)
        kn_gpu_alloc = kn.get("gpu_allocatable", 0)

        # GPU 型号过滤
        if gpu_model and kn_gpu_model != gpu_model:
            continue

        # vGPU 配置
        if (not resource_type or resource_type == "vGPU") and kn_gpu_alloc > 0:
            configs.append(ResourceConfigResponse(
                node_id=kn_name,
                node_name=kn_name,
                node_type=kn_type,
                resource_type="vGPU",
                gpu_model=kn_gpu_model,
                gpu_memory=0,
                cpu_model=f"{cpu_cores}核 {mem_gb}G",
                cpu_cores=cpu_cores,
                memory=mem_gb,
                disk=50,
                disk_expandable=0,
                network_desc="K8s 集群内网",
                gpu_available=kn_gpu_alloc,
                gpu_total=kn_gpu_count,
                hourly_price=1.0,
                region="",
            ))

        # 无卡配置
        if not resource_type or resource_type == "no_gpu":
            configs.append(ResourceConfigResponse(
                node_id=kn_name,
                node_name=kn_name,
                node_type=kn_type,
                resource_type="no_gpu",
                gpu_model=kn_gpu_model,
                gpu_memory=0,
                cpu_model=f"{max(1, cpu_cores // 4)}核 {max(1, mem_gb // 4)}G",
                cpu_cores=max(1, cpu_cores // 4),
                memory=max(1, mem_gb // 4),
                disk=50,
                disk_expandable=0,
                network_desc="K8s 集群内网",
                gpu_available=100,
                gpu_total=0,
                hourly_price=0.1,
                region="",
            ))

    return {"list": configs, "total": len(configs)}


# ========== 实例 CRUD ==========

@router.get("", response_model=PaginatedResponse, summary="获取实例列表")
async def list_instances(
    page: int = 1,
    size: int = 20,
    status: Optional[str] = None,
    search: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取当前用户的GPU实例列表"""
    query = select(Instance).where(Instance.user_id == current_user.id)
    if status:
        query = query.where(Instance.status == status)
    if search:
        query = query.where(Instance.name.ilike(f"%{search}%"))

    count_result = await db.execute(query)
    total = len(count_result.scalars().all())

    query = query.order_by(Instance.created_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    instances = result.scalars().all()

    return PaginatedResponse(
        list=[InstanceResponse.model_validate(i) for i in instances],
        total=total,
        page=page,
        size=size
    )


@router.post("", summary="创建容器实例")
async def create_instance(
    instance_data: InstanceCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    创建容器实例 - 通过 K8s API 获取节点信息，生成 Deployment YAML 下发。
    """
    logger.info(
        f"创建实例 - 用户: {current_user.id}, 名称: {instance_data.name}, "
        f"GPU: {instance_data.gpu_count}, 节点类型: {instance_data.node_type}, "
        f"node_id(K8s节点): {instance_data.node_id}"
    )

    # 从 K8s 实时获取节点信息
    k8s = get_k8s_client()
    kn = k8s.get_node(str(instance_data.node_id))
    if not kn:
        raise HTTPException(status_code=404, detail=f"K8s 节点 {instance_data.node_id} 不存在或不可达")

    nd_name = kn.get("name", str(instance_data.node_id))
    nd_cpu = _parse_cpu(kn.get("cpu_capacity", "4"))
    nd_mem = _parse_memory_gb(kn.get("memory_capacity", "8Gi"))
    nd_gpu_alloc = kn.get("gpu_allocatable", 0)
    labels = kn.get("labels", {})
    nd_gpu_model = labels.get("nvidia.com/gpu.product", "N/A")
    nd_hourly = 1.0 if nd_gpu_alloc > 0 else 0.1

    # GPU 校验 (无卡启动跳过)
    if instance_data.resource_type != "no_gpu":
        total_gpu = instance_data.gpu_count * instance_data.instance_count
        if nd_gpu_alloc < total_gpu:
            raise HTTPException(
                status_code=400,
                detail=f"GPU不足: 需要{total_gpu}, 可用{nd_gpu_alloc}"
            )

    if current_user.balance < 0:
        raise HTTPException(status_code=400, detail="余额不足")

    # 镜像：优先用前端传来的 image_url，其次从 app_images 表查找
    image_url = instance_data.image_url
    valid_image_id = None
    if instance_data.image_id:
        try:
            img_r = await db.execute(select(AppImage).where(AppImage.id == instance_data.image_id))
            image = img_r.scalar_one_or_none()
            if image:
                if not image_url:
                    image_url = image.image_url or f"{image.name}:{image.tag}"
                logger.info(f"镜像已匹配 - {image.name}:{image.tag}, URL: {image_url}")
            else:
                logger.warning(f"image_id {instance_data.image_id} 在 app_images 表中不存在")
        except Exception as e:
            logger.warning(f"查询镜像异常: {e}")
    if not image_url:
        image_url = "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-devel"
        logger.warning(f"未获取到镜像地址，使用默认: {image_url}")

    env_vars_json = None
    if instance_data.env_vars:
        env_vars_json = json.dumps([ev.dict() for ev in instance_data.env_vars], ensure_ascii=False)
    storage_json = None
    if instance_data.storage_mounts:
        storage_json = json.dumps([sm.dict() for sm in instance_data.storage_mounts], ensure_ascii=False)

    gpu_count = instance_data.gpu_count if instance_data.resource_type != "no_gpu" else 0
    instance = Instance(
        user_id=current_user.id,
        node_id=None,  # 不再关联 DB nodes 表
        node_name=nd_name,
        name=instance_data.name,
        gpu_count=gpu_count,
        gpu_model=instance_data.gpu_model or nd_gpu_model,
        cpu_cores=nd_cpu,
        memory=nd_mem,
        disk=50,
        resource_type=instance_data.resource_type,
        node_type=instance_data.node_type,
        instance_count=instance_data.instance_count,
        image_id=valid_image_id,
        image_url=image_url,
        startup_command=instance_data.startup_command,
        env_vars=env_vars_json,
        storage_mounts=storage_json,
        pip_source=instance_data.pip_source,
        conda_source=instance_data.conda_source,
        apt_source=instance_data.apt_source,
        billing_type=instance_data.billing_type,
        hourly_price=nd_hourly * (gpu_count if gpu_count > 0 else 1),
        auto_shutdown_type=instance_data.auto_shutdown_type,
        auto_shutdown_minutes=instance_data.auto_shutdown_minutes,
        auto_shutdown_time=instance_data.auto_shutdown_time,
        auto_release_type=instance_data.auto_release_type,
        auto_release_minutes=instance_data.auto_release_minutes,
        status="creating",
    )

    db.add(instance)
    await db.commit()
    await db.refresh(instance)
    logger.info(f"实例记录已创建 - ID: {instance.id}, 节点: {nd_name}")

    # 后台: 生成 Deployment YAML -> 调用 K8s -> 等待 Pod 就绪
    async def create_k8s_resources(inst_id, k8s_node_name, nd_type):
        pod_manager = get_pod_manager()
        user_envs = None
        if instance_data.env_vars:
            user_envs = [{"key": ev.key, "value": ev.value} for ev in instance_data.env_vars]
        user_storage = None
        if instance_data.storage_mounts:
            user_storage = [sm.dict() for sm in instance_data.storage_mounts]

        k8s_result = await pod_manager.create_instance(
            instance_id=str(inst_id),
            instance_name=instance_data.name,
            user_id=str(current_user.id),
            image=image_url,
            gpu_count=gpu_count,
            cpu_cores=max(1, nd_cpu // max(1, instance_data.instance_count)),
            memory_gb=max(2, nd_mem // max(1, instance_data.instance_count)),
            disk_gb=50,
            node_name=k8s_node_name,
            node_type=nd_type,
            env_vars=user_envs,
            startup_command=instance_data.startup_command,
            storage_mounts=user_storage,
            instance_count=instance_data.instance_count,
            pip_source=instance_data.pip_source,
            conda_source=instance_data.conda_source,
            apt_source=instance_data.apt_source,
        )

        final_status = "error"
        pod_ip = ""

        if k8s_result.get("success"):
            k8s_cli = get_k8s_client()
            logger.info(f"开始轮询 Pod 就绪状态 - 实例: {inst_id}")
            poll_result = k8s_cli.wait_for_pod_ready(
                namespace="lmaicloud",
                label_selector=f"instance-id={inst_id}",
                timeout=120,
                interval=3,
            )
            logger.info(f"轮询结果 - 实例: {inst_id}, ready: {poll_result['ready']}, message: {poll_result.get('message', '')}")
            if poll_result["ready"]:
                final_status = "running"
                pod_ip = poll_result.get("pod_ip", "")
                logger.info(f"Pod 就绪 - 实例: {inst_id}, pod: {poll_result['pod_name']}")
            else:
                final_status = "error"
                logger.error(f"Pod 未就绪 - 实例: {inst_id}, {poll_result['message']}")
        else:
            logger.error(f"K8s创建失败: {inst_id}, err: {k8s_result.get('error')}")

        async with AsyncSessionLocal() as session:
            stmt = select(Instance).where(Instance.id == inst_id)
            res = await session.execute(stmt)
            inst = res.scalar_one_or_none()
            if inst:
                inst.status = final_status
                inst.ssh_host = k8s_result.get("ssh_host", "")
                inst.ssh_port = k8s_result.get("ssh_port")
                inst.ssh_password = k8s_result.get("ssh_password", "")
                inst.internal_ip = pod_ip or k8s_result.get("internal_ip", "")
                inst.deployment_yaml = k8s_result.get("deployment_yaml", "")
                if final_status == "running":
                    inst.started_at = datetime.utcnow()
                await session.commit()
                logger.info(f"实例状态已更新 - {inst_id}: {final_status}")
            try:
                await broadcast_instance_status(
                    str(inst_id), str(current_user.id),
                    inst.status if inst else "error"
                )
            except Exception:
                pass

    background_tasks.add_task(create_k8s_resources, instance.id, nd_name, instance_data.node_type)
    return instance


@router.get("/{instance_id}", response_model=InstanceResponse)
async def get_instance(
    instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取实例详情"""
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    return instance


@router.post("/{instance_id}/start")
async def start_instance(
    instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """启动实例"""
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    if instance.status not in ("stopped", "error"):
        raise HTTPException(status_code=400, detail=f"当前状态 {instance.status} 无法启动")

    pod_manager = get_pod_manager()
    success = await pod_manager.start_instance(str(instance.id))
    if success:
        instance.status = "starting"
        instance.started_at = datetime.utcnow()
        await db.commit()
        return {"message": "实例启动中"}
    raise HTTPException(status_code=500, detail="启动失败")


@router.post("/{instance_id}/stop")
async def stop_instance(
    instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """停止实例"""
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    if instance.status != "running":
        raise HTTPException(status_code=400, detail=f"当前状态 {instance.status} 无法停止")

    pod_manager = get_pod_manager()
    success = await pod_manager.stop_instance(str(instance.id))
    if success:
        instance.status = "stopped"
        await db.commit()
        return {"message": "实例已停止"}
    raise HTTPException(status_code=500, detail="停止失败")


@router.delete("/{instance_id}")
async def release_instance(
    instance_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """释放实例"""
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    gpu_count = instance.gpu_count
    instance.status = "releasing"
    await db.commit()

    async def do_release(inst_id, gcount):
        pod_manager = get_pod_manager()
        await pod_manager.release_instance(str(inst_id))
        async with AsyncSessionLocal() as s:
            r = await s.execute(select(Instance).where(Instance.id == inst_id))
            inst = r.scalar_one_or_none()
            if inst:
                inst.status = "released"
                inst.release_at = datetime.utcnow()
                await s.commit()

    background_tasks.add_task(do_release, instance.id, gpu_count)
    return {"message": "实例释放中"}


@router.post("/{instance_id}/renew")
async def renew_instance(
    instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """续费实例"""
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    # 简单续费：延长 expired_at
    from datetime import timedelta
    if instance.expired_at:
        instance.expired_at = instance.expired_at + timedelta(hours=1)
    else:
        instance.expired_at = datetime.utcnow() + timedelta(hours=1)
    await db.commit()
    return {"message": "续费成功", "expired_at": str(instance.expired_at)}
