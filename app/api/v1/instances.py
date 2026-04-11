"""
容器实例 API

节点/Pod/Deployment/Service 等 K8s 资源全部直接和 K8s API 交互，
不再查询 DB 的 nodes 表。Instance 记录仍写入 DB 用于计费和用户管理。
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db, AsyncSessionLocal
from app.models import User, Instance, InstanceStatus, AppImage, Order, OrderType, OrderStatus, OpenClawInstance, UserRole
from app.schemas import (
    InstanceCreate, InstanceResponse, ResourceConfigResponse, PaginatedResponse,
    InstanceRename,
)
from app.utils.auth import get_current_user
from app.services.k8s_client import get_k8s_client
from app.services.pod_manager import get_pod_manager, PodManager
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
        return InstanceStatus.STOPPED

    if ready_replicas >= replicas and replicas > 0:
        return InstanceStatus.RUNNING

    # 检查 Deployment 明确失败条件
    for cond in conditions:
        cond_type = cond.get("type", "")
        cond_status = cond.get("status", "")
        cond_reason = cond.get("reason", "")
        # ProgressDeadlineExceeded: Deployment 已超过滚动更新期限
        if cond_type == "Progressing" and cond_status == "False":
            return InstanceStatus.ERROR
        # ReplicaFailure: Pod 无法创建
        if cond_type == "ReplicaFailure":
            return InstanceStatus.ERROR

    # 若 DB 状态是 creating，Pod 尚在创建过程中（ContainerCreating 等），保持 creating
    if db_status == InstanceStatus.CREATING:
        # 除非 Deployment 条件明确失败（上面已处理），否则保持 creating
        if available_replicas == 0 and updated_replicas == 0:
            # ReplicaSet 都没创建出来，仍可能在调度中，给时间
            return InstanceStatus.CREATING
        return InstanceStatus.CREATING

    # 有部分可用副本但尚未全部就绪 → 启动中
    if available_replicas > 0:
        return InstanceStatus.STARTING

    # 没有可用副本且没有 updated_replicas → 大概率 Pod 无法调度/创建
    if available_replicas == 0 and updated_replicas == 0:
        return InstanceStatus.ERROR

    return InstanceStatus.STARTING


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
    ACTIVE_STATUSES = {InstanceStatus.RUNNING, InstanceStatus.CREATING, InstanceStatus.STARTING, InstanceStatus.STOPPING}
    # 创建超时阈值：DB 状态仍为 creating 超过此时间 → 视为超时 error
    CREATING_TIMEOUT = timedelta(minutes=5)

    # k8s_dep_map: instance_id → 完整 deployment 解析结果
    k8s_dep_map: dict = {}
    k8s_queried = False
    # pod_metrics_map: pod_name_prefix → metrics
    pod_metrics_map: dict = {}
    active_instances = [i for i in instances if i.status in ACTIVE_STATUSES]
    if active_instances:
        try:
            k8s = get_k8s_client()
            if k8s.is_connected and not k8s.circuit_open:
                deployments = await asyncio.to_thread(
                    k8s.list_deployments,
                    label_selector="app=gpu-instance",
                    all_namespaces=True,
                )
                for dep in deployments:
                    labels = dep.get("labels") or {}
                    annotations = dep.get("annotations") or {}
                    iid = labels.get("instance-id") or annotations.get("lmaicloud/instance-id")
                    if not iid:
                        continue
                    k8s_dep_map[iid] = dep
                k8s_queried = True

                # 获取 Pod metrics（CPU/内存）- 跨命名空间
                try:
                    pod_metrics = await asyncio.to_thread(
                        k8s.list_pod_metrics, all_namespaces=True
                    )
                    for pm in pod_metrics:
                        pod_metrics_map[pm["name"]] = pm
                except Exception as e:
                    logger.warning(f"获取 Pod metrics 失败（不影响主列表）: {e}")
        except Exception as e:
            logger.warning(f"K8s Deployment 状态对齐失败（返回 DB 状态）: {e}")

    now_utc = datetime.now(timezone.utc)

    # 构建响应列表，注入 K8s 真实状态 + Deployment 信息
    resp_list = []
    db_status_changed = False   # 跟踪是否需要回写 DB
    for inst in instances:
        inst_dict = InstanceResponse.model_validate(inst).model_dump()
        inst_id = str(inst.id)

        dep = k8s_dep_map.get(inst_id) if k8s_queried else None
        if inst.status in ACTIVE_STATUSES and k8s_queried:
            if dep:
                # Deployment 存在 → 使用 _derive_instance_status 推导真实状态
                derived = _derive_instance_status(dep, db_status=inst.status)
                inst_dict["status"] = derived
                # 状态与 DB 不一致时回写，确保下次查询即使 K8s 不可用也能返回正确状态
                if derived != inst.status:
                    inst.status = derived
                    if derived == InstanceStatus.RUNNING and not inst.started_at:
                        inst.started_at = datetime.utcnow()
                    db_status_changed = True
            elif inst.status == InstanceStatus.CREATING:
                # 创建中 Deployment 尚不存在（后台任务可能还没跑完）
                created = inst.created_at.replace(tzinfo=timezone.utc) if inst.created_at and inst.created_at.tzinfo is None else inst.created_at
                if created and (now_utc - created) > CREATING_TIMEOUT:
                    inst_dict["status"] = InstanceStatus.ERROR  # 超时
                    inst.status = InstanceStatus.ERROR
                    db_status_changed = True
                # else: 保持 creating
            elif inst.status in {InstanceStatus.RUNNING, InstanceStatus.STARTING}:
                # Deployment 在 K8s 中已不存在 → 标记为 error（Pod/Deployment 已消失）
                inst_dict["status"] = InstanceStatus.ERROR
                inst.status = InstanceStatus.ERROR
                db_status_changed = True
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

        # 附加 Pod metrics（CPU/内存监控）
        prefix = f"inst-{inst_id[:8]}"
        cpu_mc = None
        mem_bytes = None
        for pname, pm in pod_metrics_map.items():
            if pname.startswith(prefix):
                cpu_mc = (cpu_mc or 0) + pm["cpu_usage_millicores"]
                mem_bytes = (mem_bytes or 0) + pm["memory_usage_bytes"]
        inst_dict["cpu_usage_millicores"] = cpu_mc
        inst_dict["memory_usage_bytes"] = mem_bytes

        resp_list.append(inst_dict)

    # K8s 状态与 DB 不一致时回写，避免下次 K8s 查询失败时返回过时状态
    if db_status_changed:
        try:
            await db.commit()
        except Exception:
            pass  # 回写失败不影响响应，同步任务会补救

    return PaginatedResponse(
        list=resp_list,
        total=total,
        page=page,
        size=size
    )


@router.post("", summary="创建容器实例")
async def create_instance(
    instance_data: InstanceCreate,
    request: Request,
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

    # 实例配额校验
    inst_count_q = await db.execute(
        select(func.count(Instance.id)).where(
            Instance.user_id == current_user.id,
            Instance.status.notin_(['released', 'error'])
        )
    )
    oc_count_q = await db.execute(
        select(func.count(OpenClawInstance.id)).where(
            OpenClawInstance.user_id == current_user.id,
            OpenClawInstance.status.notin_(['released', 'error'])
        )
    )
    current_total = (inst_count_q.scalar() or 0) + (oc_count_q.scalar() or 0)
    quota = getattr(current_user, 'instance_quota', None) or 20
    if current_total + instance_data.instance_count > quota:
        raise HTTPException(
            status_code=400,
            detail=f"实例配额不足：已使用 {current_total}/{quota}，无法再创建 {instance_data.instance_count} 个实例"
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

    unit_price = nd_hourly * (instance_data.gpu_count if instance_data.resource_type != "no_gpu" else 1)
    if instance_data.billing_type in ('monthly', 'yearly'):
        period_price = unit_price * 24 * (30 if instance_data.billing_type == 'monthly' else 365)
        billing_label = '包月' if instance_data.billing_type == 'monthly' else '包年'
        if current_user.balance < period_price:
            raise HTTPException(status_code=400, detail="{}需要 ¥{:.2f}，余额不足".format(billing_label, period_price))
    else:
        if current_user.balance <= 0:
            raise HTTPException(status_code=400, detail=f"余额不足，请充值后再创建按量计费实例")

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
    user_ns = PodManager.user_namespace(str(current_user.id))

    # 使用用户选择的规格，若未传则回退到节点真实值
    user_cpu = instance_data.cpu_cores if instance_data.cpu_cores else nd_cpu
    user_mem = instance_data.memory_gb if instance_data.memory_gb else nd_mem

    instance = Instance(
        user_id=current_user.id,
        node_id=None,  # 不再关联 DB nodes 表
        node_name=nd_name,
        name=instance_data.name,
        namespace=user_ns,
        gpu_count=gpu_count,
        gpu_model=instance_data.gpu_model or nd_gpu_model,
        cpu_cores=user_cpu,
        memory=user_mem,
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
        status=InstanceStatus.CREATING,
    )

    db.add(instance)
    await db.commit()
    await db.refresh(instance)
    logger.info(f"实例记录已创建 - ID: {instance.id}, 节点: {nd_name}")

    # 记录审计日志
    try:
        from app.api.v1.audit_log import create_audit_log, get_client_ip
        from app.models import AuditAction, AuditResourceType
        await create_audit_log(
            db, current_user.id, AuditAction.CREATE, AuditResourceType.INSTANCE,
            resource_id=str(instance.id), resource_name=instance.name,
            detail=f"GPU:{gpu_count}, 节点:{nd_name}, 镜像:{image_url}",
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"记录创建实例日志失败: {e}")

    # 创建订单记录 + 包月/包年首期扣费
    try:
        billing_cycle = instance.billing_type or 'hourly'
        if billing_cycle in ('monthly', 'yearly'):
            # 包月/包年：首期扣费 + 初始化 expired_at
            period_price = instance.hourly_price * 24 * (30 if billing_cycle == 'monthly' else 365)
            current_user.balance -= period_price
            instance.expired_at = datetime.utcnow() + (
                relativedelta(months=1) if billing_cycle == 'monthly' else relativedelta(years=1)
            )
            create_order = Order(
                user_id=current_user.id,
                instance_id=instance.id,
                type=OrderType.RENEW,
                amount=-period_price,
                status=OrderStatus.PAID,
                paid_at=datetime.utcnow(),
                product_name=f"容器实例 - {instance.name}",
                billing_cycle=billing_cycle,
                description="创建容器实例 - {} ({}首期)".format(instance.name, '包月' if billing_cycle == 'monthly' else '包年'),
            )
            logger.info(f"包月/包年首期扣费 - 用户: {current_user.id}, 金额: {period_price}, 到期: {instance.expired_at}")
        else:
            # 按量计费：不扣费，仅记录
            create_order = Order(
                user_id=current_user.id,
                instance_id=instance.id,
                type=OrderType.CREATE,
                amount=0,
                status=OrderStatus.PAID,
                paid_at=datetime.utcnow(),
                product_name=f"容器实例 - {instance.name}",
                billing_cycle=billing_cycle,
                description=f"创建容器实例 - {instance.name} (按量计费)",
            )
        db.add(create_order)
        await db.commit()
    except Exception as e:
        logger.warning(f"创建订单记录失败: {e}")

    # 计费说明：按量计费创建时不扣费，等进入 RUNNING 后由定时任务按实际运行时长计费
    # 包月/包年创建时已扣首期费用并设置 expired_at，到期由定时任务自动续费

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
            cpu_cores=max(1, user_cpu // max(1, instance_data.instance_count)),
            memory_gb=max(2, user_mem // max(1, instance_data.instance_count)),
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
            namespace=user_ns,
        )

        # ── 1) K8s 创建失败 → 直接 error ──
        if not k8s_result.get("success"):
            logger.error(f"K8s创建失败: {inst_id}, err: {k8s_result.get('error')}")
            async with AsyncSessionLocal() as session:
                stmt = select(Instance).where(Instance.id == inst_id)
                res = await session.execute(stmt)
                inst = res.scalar_one_or_none()
                if inst:
                    inst.status = InstanceStatus.ERROR
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
            namespace=user_ns,
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
                    inst.status = InstanceStatus.RUNNING
                    inst.started_at = datetime.utcnow()
                    # 仅按量计费实例设置 last_billed_at，包月/包年走 expired_at 机制
                    if getattr(inst, 'billing_type', 'hourly') not in ('monthly', 'yearly'):
                        inst.last_billed_at = datetime.utcnow()
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
    query = select(Instance).where(Instance.id == instance_id)
    # 管理员可查看任意实例，普通用户只能查看自己的
    if current_user.role != UserRole.ADMIN:
        query = query.where(Instance.user_id == current_user.id)
    result = await db.execute(query)
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
            inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
            dep = await asyncio.to_thread(k8s.get_deployment, dep_name, inst_ns)
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
                namespace=inst_ns,
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
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """启动实例"""
    query = select(Instance).where(Instance.id == instance_id)
    if current_user.role != UserRole.ADMIN:
        query = query.where(Instance.user_id == current_user.id)
    result = await db.execute(query)
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    if instance.status not in (InstanceStatus.STOPPED, InstanceStatus.ERROR, InstanceStatus.EXPIRED):
        raise HTTPException(status_code=400, detail=f"当前状态 {instance.status} 无法启动")

    # 余额检查：区分计费类型
    bt = instance.billing_type or 'hourly'
    if bt in ('monthly', 'yearly'):
        # 包月/包年：检查是否在有效期内
        if instance.expired_at and datetime.utcnow() > instance.expired_at:
            # 已过期，检查续费余额
            renew_price = instance.hourly_price * 24 * (30 if bt == 'monthly' else 365)
            if current_user.balance < renew_price:
                raise HTTPException(
                    status_code=400,
                    detail="订阅已过期且余额不足续费，需要 ¥{:.2f}".format(renew_price)
                )
    else:
        # 按量计费：余额必须大于 0
        if current_user.balance <= 0:
            raise HTTPException(status_code=400, detail=f"余额不足，请充值后再启动按量计费实例")

    pod_manager = get_pod_manager()
    inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
    success = await asyncio.to_thread(pod_manager.start_instance, str(instance.id), inst_ns)
    if not success:
        raise HTTPException(status_code=500, detail="启动失败")

    instance.status = InstanceStatus.STARTING
    instance.started_at = datetime.utcnow()
    await db.commit()

    # 记录审计日志
    try:
        from app.api.v1.audit_log import create_audit_log, get_client_ip
        from app.models import AuditAction, AuditResourceType
        await create_audit_log(
            db, current_user.id, AuditAction.START, AuditResourceType.INSTANCE,
            resource_id=str(instance.id), resource_name=instance.name,
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"记录启动实例日志失败: {e}")

    # 后台轮询 Pod 就绪状态并更新 DB
    async def wait_for_running(inst_id, user_id):
        k8s_cli = get_k8s_client()
        logger.info(f"开始轮询启动后 Pod 就绪状态 - 实例: {inst_id}")
        poll_result = await asyncio.to_thread(
            k8s_cli.wait_for_pod_ready,
            namespace=inst_ns,
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
                    inst.status = InstanceStatus.RUNNING
                    # 仅按量计费实例设置 last_billed_at，包月/包年走 expired_at 机制
                    if getattr(inst, 'billing_type', 'hourly') not in ('monthly', 'yearly'):
                        inst.last_billed_at = datetime.utcnow()
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


@router.patch("/{instance_id}/rename")
async def rename_instance(
    instance_id: str,
    req: InstanceRename,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """修改实例名称"""
    query = select(Instance).where(Instance.id == instance_id)
    if current_user.role != UserRole.ADMIN:
        query = query.where(Instance.user_id == current_user.id)
    result = await db.execute(query)
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    old_name = instance.name
    new_name = req.name.strip()
    instance.name = new_name
    await db.commit()

    # 同步更新 K8s Deployment annotation
    try:
        k8s = get_k8s_client()
        short_id = str(instance.id)[:8]
        deploy_name = f"inst-{short_id}"
        inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
        patch_body = {
            "metadata": {
                "annotations": {"lmaicloud/instance-name": new_name}
            },
        }
        await asyncio.to_thread(k8s.update_deployment, deploy_name, inst_ns, patch_body)
    except Exception as e:
        logger.warning(f"同步 K8s Deployment 名称失败(已忽略): {e}")

    # 审计日志
    try:
        from app.api.v1.audit_log import create_audit_log, get_client_ip
        from app.models import AuditAction, AuditResourceType
        await create_audit_log(
            db, current_user.id, AuditAction.UPDATE, AuditResourceType.INSTANCE,
            resource_id=str(instance.id), resource_name=instance.name,
            detail=f"修改名称: {old_name} -> {instance.name}",
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"记录修改名称日志失败: {e}")

    return {"message": "名称已修改", "name": instance.name}


@router.post("/{instance_id}/stop")
async def stop_instance(
    instance_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """停止实例"""
    query = select(Instance).where(Instance.id == instance_id)
    if current_user.role != UserRole.ADMIN:
        query = query.where(Instance.user_id == current_user.id)
    result = await db.execute(query)
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    if instance.status != InstanceStatus.RUNNING:
        raise HTTPException(status_code=400, detail=f"当前状态 {instance.status} 无法停止")

    # 停机前即时结算
    try:
        from app.api.v1.billing import settle_instance_billing
        user_result = await db.execute(select(User).where(User.id == current_user.id))
        user_obj = user_result.scalar_one()
        await settle_instance_billing(instance, user_obj, db, "gpu", "停机结算")
    except Exception as e:
        logger.warning(f"停机结算失败: {e}")

    pod_manager = get_pod_manager()
    inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
    success = await asyncio.to_thread(pod_manager.stop_instance, str(instance.id), inst_ns)
    if success:
        instance.status = InstanceStatus.STOPPED
        await db.commit()
        # 记录审计日志
        try:
            from app.api.v1.audit_log import create_audit_log, get_client_ip
            from app.models import AuditAction, AuditResourceType
            await create_audit_log(
                db, current_user.id, AuditAction.STOP, AuditResourceType.INSTANCE,
                resource_id=str(instance.id), resource_name=instance.name,
                ip_address=get_client_ip(request),
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"记录停止实例日志失败: {e}")
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
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """强制删除实例：无论当前状态，直接清理 K8s 资源并从 DB 标记为已删除"""
    return await _do_force_delete(instance_id, current_user, db, background_tasks, request)


@router.post("/{instance_id}/force", summary="强制删除实例（POST兼容入口，适配严格反向代理环境）")
async def force_delete_instance_post(
    instance_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """与 DELETE /{instance_id}/force 完全等价，供不支持 DELETE 的反向代理环境使用"""
    return await _do_force_delete(instance_id, current_user, db, background_tasks, request)


async def _do_force_delete(instance_id: str, current_user: User, db: AsyncSession, background_tasks: BackgroundTasks, request: Request = None):
    """
    强制删除核心逻辑

    先更新 DB 状态为 released（确保立即响应），
    再通过后台任务清理 K8s 资源（Deployment + Service），避免 K8s 操作阻塞请求。
    """
    query = select(Instance).where(Instance.id == instance_id)
    if current_user.role != UserRole.ADMIN:
        query = query.where(Instance.user_id == current_user.id)
    result = await db.execute(query)
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    # 删除前即时结算
    try:
        from app.api.v1.billing import settle_instance_billing
        user_result = await db.execute(select(User).where(User.id == current_user.id))
        user_obj = user_result.scalar_one()
        await settle_instance_billing(instance, user_obj, db, "gpu", "删除结算")
    except Exception as e:
        logger.warning(f"强制删除结算失败: {e}")

    # 1. 先更新 DB 状态（立即响应，避免 K8s 操作阻塞导致 NetworkError）
    inst_name = instance.name
    try:
        instance.status = InstanceStatus.RELEASED
        instance.release_at = datetime.utcnow()
        await db.commit()
        logger.info(f"实例 {instance_id} DB 状态已更新为 released, user={current_user.id}")
        # 记录审计日志
        try:
            from app.api.v1.audit_log import create_audit_log, get_client_ip
            from app.models import AuditAction, AuditResourceType
            await create_audit_log(
                db, current_user.id, AuditAction.DELETE, AuditResourceType.INSTANCE,
                resource_id=str(instance_id), resource_name=inst_name,
                detail="强制删除",
                ip_address=get_client_ip(request) if request else None,
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"记录强制删除日志失败: {e}")
    except Exception as e:
        logger.error(f"强制删除 DB 更新失败: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"数据库更新失败: {e}")

    # 2. 后台任务: 强制清理 K8s 资源（Deployment + Pod + Service），忽略所有错误
    async def cleanup_k8s(inst_id: str):
        try:
            pod_manager = get_pod_manager()
            inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
            await asyncio.to_thread(pod_manager.force_cleanup_instance, str(inst_id), inst_ns)
            logger.info(f"实例 {inst_id} K8s 资源已强制清理（Deployment + Pod + Service）")
        except Exception as e:
            logger.warning(f"强制删除 K8s 资源异常（已忽略）: {e}")

    background_tasks.add_task(cleanup_k8s, instance_id)
    return {"message": "实例已强制删除"}


@router.delete("/{instance_id}")
async def release_instance(
    instance_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除实例"""
    query = select(Instance).where(Instance.id == instance_id)
    if current_user.role != UserRole.ADMIN:
        query = query.where(Instance.user_id == current_user.id)
    result = await db.execute(query)
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")
    if instance.status in (InstanceStatus.RELEASING, InstanceStatus.RELEASED):
        raise HTTPException(status_code=400, detail=f"实例已处于 {instance.status} 状态，无需重复操作")

    # 删除前即时结算
    try:
        from app.api.v1.billing import settle_instance_billing
        user_result = await db.execute(select(User).where(User.id == current_user.id))
        user_obj = user_result.scalar_one()
        await settle_instance_billing(instance, user_obj, db, "gpu", "删除结算")
    except Exception as e:
        logger.warning(f"删除结算失败: {e}")

    gpu_count = instance.gpu_count
    instance.status = InstanceStatus.RELEASING
    await db.commit()

    # 记录审计日志
    try:
        from app.api.v1.audit_log import create_audit_log, get_client_ip
        from app.models import AuditAction, AuditResourceType
        await create_audit_log(
            db, current_user.id, AuditAction.DELETE, AuditResourceType.INSTANCE,
            resource_id=str(instance.id), resource_name=instance.name,
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        logger.warning(f"记录删除实例日志失败: {e}")

    async def do_release(inst_id, gcount, user_id):
        # K8s 清理: 失败不阻塞，DB 状态必须更新
        try:
            pod_manager = get_pod_manager()
            inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
            await asyncio.to_thread(pod_manager.release_instance, str(inst_id), inst_ns)
        except Exception as e:
            logger.warning(f"删除 K8s 资源异常（已忽略）: {e}")
        # 无论 K8s 是否成功，都更新 DB
        try:
            async with AsyncSessionLocal() as s:
                r = await s.execute(select(Instance).where(Instance.id == inst_id))
                inst = r.scalar_one_or_none()
                if inst:
                    inst.status = InstanceStatus.RELEASED
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
    """续费实例：根据 billing_type 计算续费金额，扣费并延长 expired_at"""
    result = await db.execute(
        select(Instance).where(
            Instance.id == instance_id,
            Instance.user_id == current_user.id
        )
    )
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    bt = instance.billing_type or 'hourly'
    if bt not in ('monthly', 'yearly'):
        raise HTTPException(status_code=400, detail="按量计费实例无需手动续费")

    # 计算续费金额
    if bt == 'monthly':
        renew_price = instance.hourly_price * 24 * 30
        delta = relativedelta(months=1)
        label = '包月'
    else:
        renew_price = instance.hourly_price * 24 * 365
        delta = relativedelta(years=1)
        label = '包年'

    # 余额检查
    if current_user.balance < renew_price:
        raise HTTPException(
            status_code=400,
            detail="{}续费需要 ¥{:.2f}，余额不足".format(label, renew_price)
        )

    # 扣费
    current_user.balance -= renew_price

    # 延长有效期
    base_time = instance.expired_at if instance.expired_at and instance.expired_at > datetime.utcnow() else datetime.utcnow()
    instance.expired_at = base_time + delta

    # 如果实例已过期，恢复为 STOPPED 状态（用户可手动启动）
    if instance.status == InstanceStatus.EXPIRED:
        instance.status = InstanceStatus.STOPPED

    # 创建续费订单
    renew_order = Order(
        user_id=current_user.id,
        instance_id=instance.id,
        type=OrderType.RENEW,
        amount=-renew_price,
        status=OrderStatus.PAID,
        paid_at=datetime.utcnow(),
        product_name=f"{instance.gpu_model or 'GPU'} 容器实例",
        billing_cycle=bt,
        description="{}手动续费 - {}".format(label, instance.name),
    )
    db.add(renew_order)
    await db.commit()

    return {
        "message": "续费成功",
        "expired_at": str(instance.expired_at),
        "amount": renew_price,
        "new_balance": current_user.balance,
    }


@router.get("/{instance_id}/status", summary="获取实例 Deployment/Pod 运行状态")
async def get_instance_status(
    instance_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取实例对应 Deployment 和 Pod 的运行时状态"""
    query = select(Instance).where(Instance.id == instance_id)
    if current_user.role != UserRole.ADMIN:
        query = query.where(Instance.user_id == current_user.id)
    result = await db.execute(query)
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
            inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
            dep = await asyncio.to_thread(k8s.get_deployment, dep_name, inst_ns)
            if dep:
                dep_info = {
                    "name": dep.get("name"),
                    "replicas": dep.get("replicas") or 0,
                    "ready_replicas": dep.get("ready_replicas") or 0,
                    "available_replicas": dep.get("available_replicas") or 0,
                    "conditions": dep.get("conditions") or [],
                }
                k8s_status = _derive_instance_status(dep, db_status=instance.status)
            elif instance.status == InstanceStatus.CREATING:
                # 创建中 Deployment 尚不存在，保持 creating（除非超时）
                now_utc = datetime.now(timezone.utc)
                created = instance.created_at.replace(tzinfo=timezone.utc) if instance.created_at and instance.created_at.tzinfo is None else instance.created_at
                if created and (now_utc - created) > timedelta(minutes=5):
                    k8s_status = InstanceStatus.ERROR
            elif instance.status in {InstanceStatus.RUNNING, InstanceStatus.STARTING}:
                k8s_status = InstanceStatus.ERROR

            # 查询关联 Pod
            pods = await asyncio.to_thread(
                k8s.list_pods,
                namespace=inst_ns,
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
    """获取实例监控指标 - 通过 K8s Metrics API（等价于 kubectl top pod）"""
    query = select(Instance).where(Instance.id == instance_id)
    if current_user.role != UserRole.ADMIN:
        query = query.where(Instance.user_id == current_user.id)
    result = await db.execute(query)
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    cpu_usage_mc = None
    memory_usage_bytes = None
    try:
        k8s = get_k8s_client()
        if k8s.is_connected:
            inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
            pod_metrics = await asyncio.to_thread(
                k8s.list_pod_metrics, namespace=inst_ns
            )
            # 匹配 instance-id 标签对应的 Pod
            # Pod 名前缀: inst-{instance_id[:8]}
            prefix = f"inst-{instance_id[:8]}"
            for pm in pod_metrics:
                if pm["name"].startswith(prefix):
                    cpu_usage_mc = pm["cpu_usage_millicores"]
                    memory_usage_bytes = pm["memory_usage_bytes"]
                    break
    except Exception as e:
        logger.warning(f"获取实例 {instance_id} Pod metrics 失败: {e}")

    return {
        "instance_id": str(instance_id),
        "status": instance.status,
        "cpu_usage_millicores": cpu_usage_mc,
        "memory_usage_bytes": memory_usage_bytes,
        "gpu_util": None,
        "gpu_memory": None,
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
    query = select(Instance).where(Instance.id == instance_id)
    if current_user.role != UserRole.ADMIN:
        query = query.where(Instance.user_id == current_user.id)
    result = await db.execute(query)
    instance = result.scalar_one_or_none()
    if not instance:
        raise HTTPException(status_code=404, detail="实例不存在")

    pm = get_pod_manager()
    inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
    logs = await asyncio.to_thread(pm.get_instance_logs, instance_id, tail, inst_ns)
    if logs is None:
        return {"logs": ""}
    return {"logs": logs}
