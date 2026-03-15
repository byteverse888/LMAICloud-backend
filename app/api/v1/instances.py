"""
容器实例 API

节点/Pod/Deployment/Service 等 K8s 资源全部直接和 K8s API 交互，
不再查询 DB 的 nodes 表。Instance 记录仍写入 DB 用于计费和用户管理。
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
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


def _derive_instance_status(dep: dict, db_status: str = "") -> str:
    """
    从 Deployment 状态推导实例运行状态（以 Deployment 为权威数据源）

    规则:
    - replicas == 0  → stopped
    - ready_replicas >= replicas > 0 → running
    - Progressing condition False (ProgressDeadlineExceeded) → error
    - ReplicaFailure condition → error
    - available_replicas == 0 且 updated_replicas == 0 → error（Pod 从未创建成功）
    - db_status == "creating" 且不处于明确错误 → 保持 "creating"
    - 其他情况 → starting
    """
    replicas = dep.get("replicas") or 0
    ready_replicas = dep.get("ready_replicas") or 0
    available_replicas = dep.get("available_replicas") or 0
    updated_replicas = dep.get("updated_replicas") or 0
    conditions = dep.get("conditions") or []

    if replicas == 0:
        return "stopped"

    if ready_replicas >= replicas and replicas > 0:
        return "running"

    # 检查 Deployment 明确失败条件
    for cond in conditions:
        cond_type = cond.get("type", "")
        cond_status = cond.get("status", "")
        cond_reason = cond.get("reason", "")
        # ProgressDeadlineExceeded: Deployment 已超过滚动更新期限
        if cond_type == "Progressing" and cond_status == "False":
            return "error"
        # ReplicaFailure: Pod 无法创建
        if cond_type == "ReplicaFailure":
            return "error"

    # 若 DB 状态是 creating，Pod 尚在创建过程中（ContainerCreating 等），保持 creating
    if db_status == "creating":
        # 除非 Deployment 条件明确失败（上面已处理），否则保持 creating
        if available_replicas == 0 and updated_replicas == 0:
            # ReplicaSet 都没创建出来，仍可能在调度中，给时间
            return "creating"
        return "creating"

    # 有部分可用副本但尚未全部就绪 → 启动中
    if available_replicas > 0:
        return "starting"

    # 没有可用副本且没有 updated_replicas → 大概率 Pod 无法调度/创建
    if available_replicas == 0 and updated_replicas == 0:
        return "error"

    return "starting"


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

    k8s_nodes = await asyncio.to_thread(k8s.list_nodes)
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
    """获取当前用户的GPU实例列表，状态与 K8s 实际 Pod 状态实时对齐"""
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

    # ── K8s 状态对齐（以 Deployment 为权威数据源）──────────────────────────
    # 对 DB 中仍处于"活跃"状态（非终态）的实例，从 K8s Deployment 获取真实状态覆盖
    ACTIVE_STATUSES = {"running", "creating", "starting", "stopping"}
    # 创建超时阈值：DB 状态仍为 creating 超过此时间 → 视为超时 error
    CREATING_TIMEOUT = timedelta(minutes=5)

    # k8s_dep_map: instance_id → 完整 deployment 解析结果
    k8s_dep_map: dict = {}
    k8s_queried = False
    active_instances = [i for i in instances if i.status in ACTIVE_STATUSES]
    if active_instances:
        try:
            k8s = get_k8s_client()
            if k8s.is_connected:
                deployments = await asyncio.to_thread(
                    k8s.list_deployments,
                    namespace="lmaicloud",
                    label_selector="app=gpu-instance",
                )
                for dep in deployments:
                    labels = dep.get("labels") or {}
                    annotations = dep.get("annotations") or {}
                    iid = labels.get("instance-id") or annotations.get("lmaicloud/instance-id")
                    if not iid:
                        continue
                    k8s_dep_map[iid] = dep
                k8s_queried = True
        except Exception as e:
            logger.warning(f"K8s Deployment 状态对齐失败（返回 DB 状态）: {e}")

    now_utc = datetime.now(timezone.utc)

    # 构建响应列表，注入 K8s 真实状态 + Deployment 信息
    resp_list = []
    for inst in instances:
        inst_dict = InstanceResponse.model_validate(inst).model_dump()
        inst_id = str(inst.id)

        dep = k8s_dep_map.get(inst_id) if k8s_queried else None
        if inst.status in ACTIVE_STATUSES and k8s_queried:
            if dep:
                # Deployment 存在 → 使用 _derive_instance_status 推导真实状态
                inst_dict["status"] = _derive_instance_status(dep, db_status=inst.status)
            elif inst.status == "creating":
                # 创建中 Deployment 尚不存在（后台任务可能还没跑完）
                created = inst.created_at.replace(tzinfo=timezone.utc) if inst.created_at and inst.created_at.tzinfo is None else inst.created_at
                if created and (now_utc - created) > CREATING_TIMEOUT:
                    inst_dict["status"] = "error"  # 超时
                # else: 保持 "creating"
            elif inst.status in {"running", "starting"}:
                # Deployment 在 K8s 中已不存在 → 标记为 error（Pod/Deployment 已消失）
                inst_dict["status"] = "error"
            # stopping 状态保留（等待后台任务完成删除）

        # 附加 Deployment 运行时信息（供前端展示）
        if dep:
            inst_dict["deployment_name"] = dep.get("name", "")
            inst_dict["replicas"] = dep.get("replicas") or 0
            inst_dict["ready_replicas"] = dep.get("ready_replicas") or 0
            inst_dict["available_replicas"] = dep.get("available_replicas") or 0
        else:
            inst_dict["deployment_name"] = f"inst-{inst_id[:8]}" if inst_id else ""
            inst_dict["replicas"] = None
            inst_dict["ready_replicas"] = None
            inst_dict["available_replicas"] = None

        resp_list.append(inst_dict)

    return PaginatedResponse(
        list=resp_list,
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

    # 从 K8s 实时获取节点信息（放入线程池避免阻塞事件循环）
    k8s = get_k8s_client()
    kn = await asyncio.to_thread(k8s.get_node, str(instance_data.node_id))
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

    # 后台: 生成 Deployment YAML -> 调用 K8s -> 轮询 Pod 就绪
    async def create_k8s_resources(inst_id, k8s_node_name, nd_type):
        pod_manager = get_pod_manager()
        user_envs = None
        if instance_data.env_vars:
            user_envs = [{"key": ev.key, "value": ev.value} for ev in instance_data.env_vars]
        user_storage = None
        if instance_data.storage_mounts:
            user_storage = [sm.dict() for sm in instance_data.storage_mounts]

        # 在线程池中执行同步 K8s 操作，避免阻塞事件循环
        k8s_result = await asyncio.to_thread(
            pod_manager.create_instance,
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

        # ── 1) K8s 创建失败 → 直接 error ──
        if not k8s_result.get("success"):
            logger.error(f"K8s创建失败: {inst_id}, err: {k8s_result.get('error')}")
            async with AsyncSessionLocal() as session:
                stmt = select(Instance).where(Instance.id == inst_id)
                res = await session.execute(stmt)
                inst = res.scalar_one_or_none()
                if inst:
                    inst.status = "error"
                    inst.deployment_yaml = k8s_result.get("deployment_yaml", "")
                    await session.commit()
            try:
                await broadcast_instance_status(str(inst_id), str(current_user.id), "error")
            except Exception:
                pass
            return

        # ── 2) K8s 创建成功 → 先保存连接信息（status 仍保持 creating）──
        async with AsyncSessionLocal() as session:
            stmt = select(Instance).where(Instance.id == inst_id)
            res = await session.execute(stmt)
            inst = res.scalar_one_or_none()
            if inst:
                inst.deployment_yaml = k8s_result.get("deployment_yaml", "")
                await session.commit()
                logger.info(f"实例部署信息已保存 - {inst_id}")

        # ── 3) 轮询 Pod 就绪（120s），仅在就绪时写 running ──
        k8s_cli = get_k8s_client()
        logger.info(f"开始轮询 Pod 就绪状态 - 实例: {inst_id}")
        poll_result = await asyncio.to_thread(
            k8s_cli.wait_for_pod_ready,
            namespace="lmaicloud",
            label_selector=f"instance-id={inst_id}",
            timeout=120,
            interval=3,
        )

        if poll_result["ready"]:
            pod_ip = poll_result.get("pod_ip", "")
            logger.info(f"Pod 就绪 - 实例: {inst_id}, pod: {poll_result['pod_name']}")
            async with AsyncSessionLocal() as session:
                stmt = select(Instance).where(Instance.id == inst_id)
                res = await session.execute(stmt)
                inst = res.scalar_one_or_none()
                if inst:
                    inst.status = "running"
                    inst.started_at = datetime.utcnow()
                    inst.internal_ip = pod_ip
                    await session.commit()
                    logger.info(f"实例状态已更新 - {inst_id}: running")
            try:
                await broadcast_instance_status(str(inst_id), str(current_user.id), "running")
            except Exception:
                pass
        else:
            # 超时不写 error —— 保持 creating，由周期同步任务后续处理
            logger.info(f"Pod 未在120s内就绪，保持 creating 等待后续同步 - 实例: {inst_id}, {poll_result.get('message', '')}")

    background_tasks.add_task(create_k8s_resources, instance.id, nd_name, instance_data.node_type)
    return instance


@router.get("/{instance_id}", summary="获取实例详情（含 Deployment/Pod 运行信息）")
async def get_instance(
    instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    获取实例详情 - 以 Deployment 为权威数据源。

    返回 DB 基础信息 + K8s Deployment 状态 + 关联 Pod 信息，
    前端可直接使用 deployment_info / pod_info 展示运行状态。
    """
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    # 基础信息（DB）
    resp = InstanceResponse.model_validate(instance).model_dump()

    # ── 从 K8s 获取 Deployment + Pod 运行时信息 ──────────────────
    dep_info = None
    pod_info_list = []
    try:
        k8s = get_k8s_client()
        if k8s.is_connected:
            dep_name = f"inst-{instance_id[:8]}"
            dep = await asyncio.to_thread(k8s.get_deployment, dep_name, "lmaicloud")
            if dep:
                dep_info = {
                    "name": dep.get("name"),
                    "replicas": dep.get("replicas") or 0,
                    "ready_replicas": dep.get("ready_replicas") or 0,
                    "available_replicas": dep.get("available_replicas") or 0,
                    "updated_replicas": dep.get("updated_replicas") or 0,
                    "images": dep.get("images") or [],
                    "conditions": dep.get("conditions") or [],
                    "strategy": dep.get("strategy", "RollingUpdate"),
                    "created_at": dep.get("created_at"),
                }
                # 以 Deployment 状态覆盖 DB 状态（活跃实例）
                if instance.status in {"running", "creating", "starting", "stopping"}:
                    resp["status"] = _derive_instance_status(dep, db_status=instance.status)

            # 获取关联的 Pod 列表
            pods = await asyncio.to_thread(
                k8s.list_pods,
                namespace="lmaicloud",
                label_selector=f"instance-id={instance_id}",
            )
            for p in (pods or []):
                pod_info_list.append({
                    "name": p.get("name"),
                    "status": p.get("effective_status") or p.get("status"),
                    "ip": p.get("ip"),
                    "node_name": p.get("node_name"),
                    "restart_count": p.get("restart_count", 0),
                    "is_terminating": p.get("is_terminating", False),
                    "containers": p.get("containers", []),
                })

            # 如果 Deployment 不存在但 DB 状态是活跃的
            if not dep:
                if instance.status == "creating":
                    # 创建中 Deployment 尚不存在，保持 creating（除非超时）
                    now_utc = datetime.now(timezone.utc)
                    created = instance.created_at.replace(tzinfo=timezone.utc) if instance.created_at and instance.created_at.tzinfo is None else instance.created_at
                    if created and (now_utc - created) > timedelta(minutes=5):
                        resp["status"] = "error"
                elif instance.status in {"running", "starting"}:
                    resp["status"] = "error"
    except Exception as e:
        logger.warning(f"获取实例 {instance_id} K8s 信息失败: {e}")

    resp["deployment_info"] = dep_info
    resp["pod_info"] = pod_info_list
    resp["deployment_name"] = dep_info["name"] if dep_info else f"inst-{instance_id[:8]}"

    return resp


@router.post("/{instance_id}/start")
async def start_instance(
    instance_id: str,
    background_tasks: BackgroundTasks,
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
    success = await asyncio.to_thread(pod_manager.start_instance, str(instance.id))
    if not success:
        raise HTTPException(status_code=500, detail="启动失败")

    instance.status = "starting"
    instance.started_at = datetime.utcnow()
    await db.commit()

    # 后台轮询 Pod 就绪状态并更新 DB
    async def wait_for_running(inst_id, user_id):
        k8s_cli = get_k8s_client()
        logger.info(f"开始轮询启动后 Pod 就绪状态 - 实例: {inst_id}")
        poll_result = await asyncio.to_thread(
            k8s_cli.wait_for_pod_ready,
            namespace="lmaicloud",
            label_selector=f"instance-id={inst_id}",
            timeout=120,
            interval=3,
        )
        if poll_result["ready"]:
            logger.info(f"Pod 就绪(启动) - 实例: {inst_id}, pod: {poll_result['pod_name']}")
            async with AsyncSessionLocal() as session:
                stmt = select(Instance).where(Instance.id == inst_id)
                res = await session.execute(stmt)
                inst = res.scalar_one_or_none()
                if inst:
                    inst.status = "running"
                    pod_ip = poll_result.get("pod_ip", "")
                    if pod_ip:
                        inst.internal_ip = pod_ip
                    await session.commit()
                    logger.info(f"实例状态已更新(启动) - {inst_id}: running")
            try:
                await broadcast_instance_status(str(inst_id), str(user_id), "running")
            except Exception:
                pass
        else:
            # 超时不写 error —— 保持 starting，由周期同步任务后续处理
            logger.info(f"Pod 未在120s内就绪(启动)，保持 starting 等待后续同步 - 实例: {inst_id}")
            pass

    background_tasks.add_task(wait_for_running, instance.id, current_user.id)
    return {"message": "实例启动中"}


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
    success = await asyncio.to_thread(pod_manager.stop_instance, str(instance.id))
    if success:
        instance.status = "stopped"
        await db.commit()
        # 广播 WebSocket 通知前端
        try:
            await broadcast_instance_status(str(instance.id), str(current_user.id), "stopped")
        except Exception:
            pass
        return {"message": "实例已停止"}
    raise HTTPException(status_code=500, detail="停止失败")


@router.delete("/{instance_id}/force", summary="强制删除实例 - 必须在 DELETE /{instance_id} 之前注册")
async def force_delete_instance(
    instance_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """强制删除实例：无论当前状态，直接清理 K8s 资源并从 DB 标记为已删除"""
    return await _do_force_delete(instance_id, current_user, db, background_tasks)


@router.post("/{instance_id}/force", summary="强制删除实例（POST兼容入口，适配严格反向代理环境）")
async def force_delete_instance_post(
    instance_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """与 DELETE /{instance_id}/force 完全等价，供不支持 DELETE 的反向代理环境使用"""
    return await _do_force_delete(instance_id, current_user, db, background_tasks)


async def _do_force_delete(instance_id: str, current_user: User, db: AsyncSession, background_tasks: BackgroundTasks):
    """
    强制删除核心逻辑

    先更新 DB 状态为 released（确保立即响应），
    再通过后台任务清理 K8s 资源（Deployment + Service），避免 K8s 操作阻塞请求。
    """
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    # 1. 先更新 DB 状态（立即响应，避免 K8s 操作阻塞导致 NetworkError）
    try:
        instance.status = "released"
        instance.release_at = datetime.utcnow()
        await db.commit()
        logger.info(f"实例 {instance_id} DB 状态已更新为 released, user={current_user.id}")
    except Exception as e:
        logger.error(f"强制删除 DB 更新失败: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库更新失败: {e}")

    # 2. 后台任务: 强制清理 K8s 资源（Deployment + Pod + Service），忽略所有错误
    async def cleanup_k8s(inst_id: str):
        try:
            pod_manager = get_pod_manager()
            await asyncio.to_thread(pod_manager.force_cleanup_instance, str(inst_id))
            logger.info(f"实例 {inst_id} K8s 资源已强制清理（Deployment + Pod + Service）")
        except Exception as e:
            logger.warning(f"强制删除 K8s 资源异常（已忽略）: {e}")

    background_tasks.add_task(cleanup_k8s, instance_id)
    return {"message": "实例已强制删除"}


@router.delete("/{instance_id}")
async def release_instance(
    instance_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除实例"""
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    if instance.status in ("releasing", "released"):
        raise HTTPException(status_code=400, detail=f"实例已处于 {instance.status} 状态，无需重复操作")

    gpu_count = instance.gpu_count
    instance.status = "releasing"
    await db.commit()

    async def do_release(inst_id, gcount, user_id):
        # K8s 清理: 失败不阻塞，DB 状态必须更新
        try:
            pod_manager = get_pod_manager()
            await asyncio.to_thread(pod_manager.release_instance, str(inst_id))
        except Exception as e:
            logger.warning(f"删除 K8s 资源异常（已忽略）: {e}")
        # 无论 K8s 是否成功，都更新 DB
        try:
            async with AsyncSessionLocal() as s:
                r = await s.execute(select(Instance).where(Instance.id == inst_id))
                inst = r.scalar_one_or_none()
                if inst:
                    inst.status = "released"
                    inst.release_at = datetime.utcnow()
                    await s.commit()
                    logger.info(f"实例 {inst_id} 已删除")
        except Exception as e:
            logger.error(f"删除更新 DB 失败: {e}")
        # 广播 WebSocket 通知前端
        try:
            await broadcast_instance_status(str(inst_id), str(user_id), "released")
        except Exception:
            pass

    background_tasks.add_task(do_release, instance.id, gpu_count, current_user.id)
    return {"message": "实例删除中"}


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


@router.get("/{instance_id}/status", summary="获取实例 Deployment/Pod 运行状态")
async def get_instance_status(
    instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取实例对应 Deployment 和 Pod 的运行时状态"""
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    dep_info = None
    pod_info = None
    k8s_status = instance.status
    try:
        k8s = get_k8s_client()
        if k8s.is_connected:
            # 查询 Deployment
            dep_name = f"inst-{instance_id[:8]}"
            dep = await asyncio.to_thread(k8s.get_deployment, dep_name, "lmaicloud")
            if dep:
                dep_info = {
                    "name": dep.get("name"),
                    "replicas": dep.get("replicas") or 0,
                    "ready_replicas": dep.get("ready_replicas") or 0,
                    "available_replicas": dep.get("available_replicas") or 0,
                    "conditions": dep.get("conditions") or [],
                }
                k8s_status = _derive_instance_status(dep, db_status=instance.status)
            elif instance.status == "creating":
                # 创建中 Deployment 尚不存在，保持 creating（除非超时）
                now_utc = datetime.now(timezone.utc)
                created = instance.created_at.replace(tzinfo=timezone.utc) if instance.created_at and instance.created_at.tzinfo is None else instance.created_at
                if created and (now_utc - created) > timedelta(minutes=5):
                    k8s_status = "error"
            elif instance.status in {"running", "starting"}:
                k8s_status = "error"

            # 查询关联 Pod
            pods = await asyncio.to_thread(
                k8s.list_pods,
                namespace="lmaicloud",
                label_selector=f"instance-id={instance_id}",
            )
            if pods:
                p = pods[0]
                pod_info = {
                    "pod_name": p.get("name"),
                    "pod_status": p.get("effective_status") or p.get("status"),
                    "pod_ip": p.get("ip"),
                    "node": p.get("node_name"),
                    "restarts": p.get("restart_count", 0),
                }
    except Exception:
        pass

    return {
        "instance_id": str(instance_id),
        "status": k8s_status,
        "deployment": dep_info,
        "pod": pod_info,
    }


@router.get("/{instance_id}/metrics", summary="获取实例监控指标")
async def get_instance_metrics(
    instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取实例监控指标（Prometheus 集成后返回真实数据）"""
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    # TODO: 接入 Prometheus 后返回真实指标
    return {
        "instance_id": str(instance_id),
        "status": instance.status,
        "cpu_util": None,
        "memory_util": None,
        "gpu_util": None,
        "gpu_memory": None,
        "disk_util": None,
        "network_in": None,
        "network_out": None,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/{instance_id}/logs", summary="获取实例日志")
async def get_instance_logs(
    instance_id: str,
    tail: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取实例 Pod 日志"""
    # 验证实例归属
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    pm = get_pod_manager()
    logs = await asyncio.to_thread(pm.get_instance_logs, instance_id, tail)
    if logs is None:
        return {"logs": ""}
    return {"logs": logs}
