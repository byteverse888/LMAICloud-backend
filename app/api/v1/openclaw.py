"""
OpenClaw 实例管理 API

提供 OpenClaw AI Agent 实例的完整 CRUD、配置管理和监控接口。
"""
import asyncio
import json
from uuid import UUID
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import (
    AIUser as User, OpenClawInstance, ModelKey, Channel, OpenClawSkill,
    Order, OrderType, OrderStatus, Instance,
)
from app.schemas import (
    OpenClawInstanceCreate, OpenClawInstanceResponse, OpenClawSpecUpdate,
    ModelKeyCreate, ModelKeyUpdate, ModelKeyResponse,
    ChannelCreate, ChannelUpdate, ChannelResponse,
    SkillInstall, SkillUpdate, SkillResponse,
    MonitorModelResponse, MonitorChannelResponse, MonitorStatusResponse,
)
from app.utils.auth import get_current_user
from app.services.openclaw_manager import get_openclaw_manager, OpenClawManager
from app.services.openclaw_client import OpenClawClient, build_openclaw_url
from app.services.pod_manager import PodManager
from app.services.k8s_client import get_k8s_client
from app.config import settings
from app.tasks import enqueue_task

router = APIRouter()


# ========== 工具函数 ==========

async def _get_instance_or_404(
    instance_id: UUID, user: User, db: AsyncSession
) -> OpenClawInstance:
    """获取实例，校验归属权"""
    result = await db.execute(
        select(OpenClawInstance).where(
            OpenClawInstance.id == instance_id,
            OpenClawInstance.user_id == user.id,
        )
    )
    inst = result.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail="OpenClaw 实例不存在")
    return inst


def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


# ====================================================================
#  边缘节点列表（供创建实例时选择）
# ====================================================================

@router.get("/edge-nodes")
async def list_edge_nodes(
    current_user: User = Depends(get_current_user),
):
    """获取可用边缘节点列表（仅返回在线节点名称和基本信息）"""
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": []}
    nodes = await asyncio.to_thread(
        k8s.list_nodes,
        label_selector="node-role.kubernetes.io/edge",
    )
    result = []
    for n in nodes:
        status = n.get("status", "NotReady")
        if n.get("unschedulable"):
            continue  # 跳过维护中的节点
        result.append({
            "name": n["name"],
            "status": "online" if status == "Ready" else "offline",
            "ip": n.get("ip"),
        })
    return {"list": result}


# ====================================================================
#  实例 CRUD
# ====================================================================

@router.post("/instances", response_model=OpenClawInstanceResponse)
async def create_instance(
    req: OpenClawInstanceCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建 OpenClaw 实例（支持按量/包月/包年 + 创建时配置模型/通道/技能）"""
    from dateutil.relativedelta import relativedelta

    # ── 实例配额校验 ──
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
    if current_total + 1 > quota:
        raise HTTPException(
            status_code=400,
            detail=f"实例配额不足：已使用 {current_total}/{quota}，无法再创建 OpenClaw 实例"
        )

    # ── 规格价格表 ──
    SPEC_PRICES = {
        (1, 2):   0.06,   # 入门型
        (2, 4):   0.12,   # 通用型
        (4, 8):   0.24,   # 专业型
        (8, 16):  0.48,   # 旗舰型
    }
    hourly_price = SPEC_PRICES.get((req.cpu_cores, req.memory_gb), 0.12)

    # ── 计费模式解析 ──
    billing_type_val = "hourly"
    first_charge = 0.0
    expired_at = None
    duration_months = req.duration_months or 1

    if req.billing_type == "monthly":
        billing_type_val = "monthly"
        first_charge = round(hourly_price * 24 * 30 * duration_months, 2)
        expired_at = datetime.utcnow() + relativedelta(months=duration_months)
    elif req.billing_type == "yearly":
        billing_type_val = "yearly"
        first_charge = round(hourly_price * 24 * 365, 2)
        expired_at = datetime.utcnow() + relativedelta(years=1)

    # ── 余额检查 ──
    if req.billing_type in ("monthly", "yearly"):
        if current_user.balance < first_charge:
            raise HTTPException(
                status_code=400,
                detail=f"余额不足，需要 ¥{first_charge:.2f}，当前余额 ¥{current_user.balance:.2f}"
            )
    else:
        min_balance = hourly_price  # 至少夨1小时
        if current_user.balance < min_balance:
            raise HTTPException(status_code=400, detail=f"余额不足，至少需要 ¥{min_balance:.2f}")

    # ── 边缘节点必须指定 node_name ──
    if req.node_type == "edge" and not req.node_name:
        raise HTTPException(
            status_code=400,
            detail="边缘节点必须指定节点名称 (node_name)，请从边缘节点列表中选择"
        )

    user_ns = PodManager.user_namespace(str(current_user.id))
    image = req.image_url or settings.openclaw_default_image

    # ── DB 记录 ──
    inst = OpenClawInstance(
        user_id=current_user.id,
        name=req.name,
        status="creating",
        namespace=user_ns,
        node_name=req.node_name,
        node_type=req.node_type,
        cpu_cores=req.cpu_cores,
        memory_gb=req.memory_gb,
        disk_gb=req.disk_gb,
        image_url=image,
        port=req.port,
        billing_type=billing_type_val,
        hourly_price=hourly_price,
        expired_at=expired_at,
    )
    db.add(inst)
    await db.flush()  # 获取 inst.id

    # ── 创建时批量添加模型密钥 ──
    init_model_keys = []
    if req.model_keys:
        for mk_req in req.model_keys:
            mk = ModelKey(
                instance_id=inst.id,
                provider=mk_req.provider,
                alias=mk_req.alias,
                api_key=mk_req.api_key,
                base_url=mk_req.base_url,
                model_name=mk_req.model_name,
            )
            db.add(mk)
            init_model_keys.append({
                "provider": mk_req.provider,
                "api_key": mk_req.api_key,
                "base_url": mk_req.base_url,
                "is_active": True,
            })

    # ── 创建时批量添加通道 ──
    init_channels = []
    if req.channels:
        for ch_req in req.channels:
            ch = Channel(
                instance_id=inst.id,
                type=ch_req.type,
                name=ch_req.name,
                config=ch_req.config,
            )
            db.add(ch)
            init_channels.append({
                "type": ch_req.type,
                "name": ch_req.name,
                "config": ch_req.config,
                "is_active": True,
            })

    # ── 创建时批量添加技能 ──
    if req.skills:
        for sk_req in req.skills:
            skill = OpenClawSkill(
                instance_id=inst.id,
                name=sk_req.name,
                description=sk_req.description,
                version=sk_req.version,
                status="installing",
            )
            db.add(skill)

    await db.flush()

    # ── K8s 资源创建 ──
    mgr = get_openclaw_manager()
    k8s_result = await asyncio.to_thread(
        mgr.create_instance,
        instance_id=str(inst.id),
        user_id=str(current_user.id),
        image_url=image,
        port=req.port,
        cpu_cores=req.cpu_cores,
        memory_gb=req.memory_gb,
        disk_gb=req.disk_gb,
        node_name=req.node_name,
        node_type=req.node_type,
        storage_class=settings.openclaw_storage_class,
        edge_storage_path=settings.openclaw_edge_storage_path,
        model_keys=init_model_keys if init_model_keys else None,
        channels=init_channels if init_channels else None,
    )

    if not k8s_result.get("success"):
        inst.status = "error"
        await db.commit()
        raise HTTPException(status_code=500, detail=k8s_result.get("error", "K8s 资源创建失败"))

    inst.deployment_name = k8s_result["deployment_name"]
    inst.service_name = k8s_result["service_name"]
    inst.gateway_token = k8s_result["gateway_token"]
    inst.status = "creating"

    # ── 首期扣费（包月/包年） ──
    if first_charge > 0:
        current_user.balance -= first_charge

    await db.commit()
    await db.refresh(inst)

    # ── 审计日志 ──
    try:
        from app.api.v1.audit_log import create_audit_log
        from app.models import AuditAction, AuditResourceType
        from app.api.v1.audit_log import get_client_ip
        await create_audit_log(
            db, current_user.id, AuditAction.CREATE, AuditResourceType.OPENCLAW,
            resource_id=str(inst.id), resource_name=inst.name,
            detail=f"镜像:{image}, 规格:{req.cpu_cores}C{req.memory_gb}G, 计费:{req.billing_type}",
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"记录创建OpenClaw日志失败: {e}")

    # ── 订单记录 ──
    try:
        billing_label = {"hourly": "按量计费", "monthly": "包月", "yearly": "包年"}.get(req.billing_type, "按量计费")
        create_order = Order(
            user_id=current_user.id,
            openclaw_instance_id=inst.id,
            type=OrderType.CREATE,
            amount=first_charge,
            status=OrderStatus.PAID,
            paid_at=datetime.utcnow(),
            product_name=f"OpenClaw实例 - {inst.name}",
            billing_cycle=req.billing_type,
            description=f"创建 OpenClaw 实例 - {inst.name} ({billing_label})",
        )
        db.add(create_order)
        await db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"创建订单记录失败: {e}")

    return inst


@router.get("/instances")
async def list_instances(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户的 OpenClaw 实例列表（含 K8s Pod IP 补充）"""
    result = await db.execute(
        select(OpenClawInstance)
        .where(OpenClawInstance.user_id == current_user.id)
        .where(OpenClawInstance.status != "released")
        .order_by(OpenClawInstance.created_at.desc())
    )
    instances = result.scalars().all()

    # ── K8s Pod IP 补充：对运行/创建中的实例，从 K8s 查询 Pod IP ──
    ACTIVE_STATUSES = {"running", "creating", "starting", "stopping"}
    active_instances = [i for i in instances if i.status in ACTIVE_STATUSES]
    pod_ip_map: dict = {}  # instance_id -> ip
    if active_instances:
        try:
            k8s = get_k8s_client()
            if k8s.is_connected and not k8s.circuit_open:
                pods = await asyncio.to_thread(
                    k8s.list_pods,
                    label_selector="app=openclaw",
                    all_namespaces=True,
                )
                for p in (pods or []):
                    labels = p.get("labels") or {}
                    iid = labels.get("openclaw-instance")
                    if iid:
                        pod_ip_map[iid] = p.get("pod_ip") or p.get("ip")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"获取 OpenClaw Pod IP 失败: {e}")

    resp_list = []
    for inst in instances:
        inst_dict = OpenClawInstanceResponse.model_validate(inst).model_dump()
        # 如果 DB 的 internal_ip 为空，则用 K8s Pod IP 补充
        if not inst_dict.get("internal_ip"):
            pod_ip = pod_ip_map.get(str(inst.id))
            if pod_ip:
                inst_dict["internal_ip"] = pod_ip
                # 同时回写 DB（异步更新，不阻塞响应）
                inst.internal_ip = pod_ip
        resp_list.append(inst_dict)

    # 批量提交 DB 更新
    try:
        await db.commit()
    except Exception:
        pass

    return {"list": resp_list, "total": len(resp_list)}


@router.get("/instances/{instance_id}")
async def get_instance(
    instance_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取 OpenClaw 实例详情（含 K8s Deployment/Pod 运行信息）"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    resp = OpenClawInstanceResponse.model_validate(inst).model_dump()

    dep_info = None
    pod_info_list = []

    try:
        k8s = get_k8s_client()
        if k8s.is_connected:
            inst_ns = inst.namespace or PodManager.user_namespace(str(inst.user_id))

            # ── Deployment 信息 ──
            dep_name = inst.deployment_name or f"oc-{str(instance_id).replace('-', '')[:8]}-deploy"
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
                    "created_at": dep.get("created_at"),
                }

            # ── Pod 列表 ──
            pods = await asyncio.to_thread(
                k8s.list_pods,
                namespace=inst_ns,
                label_selector=f"openclaw-instance={instance_id}",
            )
            for p in (pods or []):
                pod_info_list.append({
                    "name": p.get("name"),
                    "status": p.get("effective_status") or p.get("status"),
                    "ip": p.get("pod_ip"),
                    "node_name": p.get("node_name"),
                    "restart_count": p.get("restart_count", 0),
                    "is_terminating": p.get("is_terminating", False),
                    "containers": p.get("containers", []),
                })

            # 补充 IP
            if pods:
                pod_ip = pods[0].get("pod_ip")
                pod_node = pods[0].get("node_name")
                if pod_ip and not resp.get("internal_ip"):
                    resp["internal_ip"] = pod_ip
                    inst.internal_ip = pod_ip
                if pod_node:
                    resp["pod_node_name"] = pod_node
    except Exception:
        pass

    resp["deployment_info"] = dep_info
    resp["pod_info"] = pod_info_list
    if not resp.get("deployment_name"):
        resp["deployment_name"] = inst.deployment_name or f"oc-{str(instance_id).replace('-', '')[:8]}-deploy"

    try:
        await db.commit()
    except Exception:
        pass

    return resp


@router.delete("/instances/{instance_id}")
async def delete_instance(
    instance_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除 OpenClaw 实例（释放所有 K8s 资源）"""
    inst = await _get_instance_or_404(instance_id, current_user, db)

    # 删除前即时结算
    try:
        from app.api.v1.billing import settle_instance_billing
        from app.models import AIUser
        user_result = await db.execute(select(AIUser).where(AIUser.id == current_user.id))
        user_obj = user_result.scalar_one()
        await settle_instance_billing(inst, user_obj, db, "openclaw", "删除结算")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"OpenClaw删除结算失败: {e}")

    inst.status = "releasing"
    await db.commit()

    mgr = get_openclaw_manager()
    await asyncio.to_thread(
        mgr.release_instance,
        instance_id=str(inst.id),
        namespace=inst.namespace,
        node_type=inst.node_type or "center",
    )

    inst.status = "released"
    await db.commit()

    # 记录审计日志
    try:
        from app.api.v1.audit_log import create_audit_log
        from app.models import AuditAction, AuditResourceType
        from app.api.v1.audit_log import get_client_ip
        await create_audit_log(
            db, current_user.id, AuditAction.DELETE, AuditResourceType.OPENCLAW,
            resource_id=str(inst.id), resource_name=inst.name,
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"记录删除OpenClaw日志失败: {e}")

    return {"detail": "实例已释放"}


@router.post("/instances/{instance_id}/start")
async def start_instance(
    instance_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """启动实例"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    if inst.status not in ("stopped", "error"):
        raise HTTPException(status_code=400, detail=f"当前状态 {inst.status} 不可启动")
    
    # 余额检查：区分计费模式
    bt = inst.billing_type.value if inst.billing_type else "hourly"
    if bt in ("monthly", "yearly"):
        # 包月/包年：检查是否过期
        if inst.expired_at and datetime.utcnow() > inst.expired_at:
            raise HTTPException(status_code=400, detail="实例已过期，请先续费")
    else:
        # 按量：至少夨1小时
        min_balance = inst.hourly_price or settings.default_gpu_hourly_price
        if current_user.balance < min_balance:
            raise HTTPException(status_code=400, detail=f"余额不足，至少需要 ¥{min_balance:.2f}")

    mgr = get_openclaw_manager()
    ok = await asyncio.to_thread(mgr.start_instance, str(inst.id), inst.namespace)
    if not ok:
        raise HTTPException(status_code=500, detail="启动失败")

    inst.status = "creating"
    inst.started_at = datetime.utcnow()
    await db.commit()

    # 记录审计日志
    try:
        from app.api.v1.audit_log import create_audit_log
        from app.models import AuditAction, AuditResourceType
        from app.api.v1.audit_log import get_client_ip
        await create_audit_log(
            db, current_user.id, AuditAction.START, AuditResourceType.OPENCLAW,
            resource_id=str(inst.id), resource_name=inst.name,
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"记录启动OpenClaw日志失败: {e}")

    return {"detail": "启动中"}


@router.post("/instances/{instance_id}/stop")
async def stop_instance(
    instance_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """停止实例"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    if inst.status not in ("running", "error"):
        raise HTTPException(status_code=400, detail=f"当前状态 {inst.status} 不可停止")

    # 停机前即时结算
    try:
        from app.api.v1.billing import settle_instance_billing
        from app.models import AIUser
        user_result = await db.execute(select(AIUser).where(AIUser.id == current_user.id))
        user_obj = user_result.scalar_one()
        await settle_instance_billing(inst, user_obj, db, "openclaw", "停机结算")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"OpenClaw停机结算失败: {e}")

    mgr = get_openclaw_manager()
    ok = await asyncio.to_thread(mgr.stop_instance, str(inst.id), inst.namespace)
    if not ok:
        raise HTTPException(status_code=500, detail="停止失败")

    inst.status = "stopped"
    await db.commit()

    # 记录审计日志
    try:
        from app.api.v1.audit_log import create_audit_log
        from app.models import AuditAction, AuditResourceType
        from app.api.v1.audit_log import get_client_ip
        await create_audit_log(
            db, current_user.id, AuditAction.STOP, AuditResourceType.OPENCLAW,
            resource_id=str(inst.id), resource_name=inst.name,
            ip_address=get_client_ip(request),
        )
        await db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"记录停止OpenClaw日志失败: {e}")

    return {"detail": "已停止"}


@router.patch("/instances/{instance_id}/spec")
async def update_instance_spec(
    instance_id: UUID,
    req: OpenClawSpecUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """动态变更实例规格（CPU/Memory），触发 Deployment 滚动更新"""
    inst = await _get_instance_or_404(instance_id, current_user, db)

    mgr = get_openclaw_manager()
    ok = await asyncio.to_thread(
        mgr.update_spec,
        instance_id=str(inst.id),
        namespace=inst.namespace,
        cpu_cores=req.cpu_cores,
        memory_gb=req.memory_gb,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="规格变更失败")

    if req.cpu_cores is not None:
        inst.cpu_cores = req.cpu_cores
    if req.memory_gb is not None:
        inst.memory_gb = req.memory_gb
    if req.disk_gb is not None:
        inst.disk_gb = req.disk_gb
    await db.commit()
    return {"detail": "规格已更新，Deployment 滚动更新中"}


# ====================================================================
#  大模型密钥管理
# ====================================================================

@router.get("/instances/{instance_id}/model-keys", response_model=list[ModelKeyResponse])
async def list_model_keys(
    instance_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取实例的大模型密钥列表"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(ModelKey).where(ModelKey.instance_id == inst.id).order_by(ModelKey.created_at.desc())
    )
    return [_mk_to_response(k) for k in result.scalars().all()]


@router.post("/instances/{instance_id}/model-keys", response_model=ModelKeyResponse)
async def add_model_key(
    instance_id: UUID,
    req: ModelKeyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """添加大模型 API 密钥"""
    inst = await _get_instance_or_404(instance_id, current_user, db)

    mk = ModelKey(
        instance_id=inst.id,
        provider=req.provider,
        alias=req.alias,
        api_key=req.api_key,
        base_url=req.base_url,
        model_name=req.model_name,
    )
    db.add(mk)
    await db.flush()

    # 热更新 K8s Secret
    await _sync_secret(inst, db)
    await db.commit()
    await db.refresh(mk)

    return _mk_to_response(mk)


@router.put("/instances/{instance_id}/model-keys/{key_id}", response_model=ModelKeyResponse)
async def update_model_key(
    instance_id: UUID,
    key_id: UUID,
    req: ModelKeyUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新大模型密钥"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(ModelKey).where(ModelKey.id == key_id, ModelKey.instance_id == inst.id)
    )
    mk = result.scalar_one_or_none()
    if not mk:
        raise HTTPException(status_code=404, detail="密钥不存在")

    if req.alias is not None:
        mk.alias = req.alias
    if req.api_key is not None:
        mk.api_key = req.api_key
    if req.base_url is not None:
        mk.base_url = req.base_url
    if req.model_name is not None:
        mk.model_name = req.model_name
    if req.is_active is not None:
        mk.is_active = req.is_active

    await _sync_secret(inst, db)
    await db.commit()
    await db.refresh(mk)
    return _mk_to_response(mk)


@router.delete("/instances/{instance_id}/model-keys/{key_id}")
async def delete_model_key(
    instance_id: UUID,
    key_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除大模型密钥"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(ModelKey).where(ModelKey.id == key_id, ModelKey.instance_id == inst.id)
    )
    mk = result.scalar_one_or_none()
    if not mk:
        raise HTTPException(status_code=404, detail="密钥不存在")

    await db.delete(mk)
    await _sync_secret(inst, db)
    await db.commit()
    return {"detail": "密钥已删除"}


def _mk_to_response(mk: ModelKey) -> ModelKeyResponse:
    return ModelKeyResponse(
        id=mk.id,
        instance_id=mk.instance_id,
        provider=mk.provider,
        alias=mk.alias,
        api_key_masked=_mask_key(mk.api_key),
        base_url=mk.base_url,
        model_name=mk.model_name,
        is_active=mk.is_active,
        last_check_at=mk.last_check_at,
        check_status=mk.check_status or "unknown",
        balance=mk.balance,
        tokens_used=mk.tokens_used or 0,
        created_at=mk.created_at,
    )


async def _sync_secret(inst: OpenClawInstance, db: AsyncSession):
    """同步所有密钥到 K8s Secret 并热更新"""
    result = await db.execute(
        select(ModelKey).where(ModelKey.instance_id == inst.id)
    )
    keys = result.scalars().all()
    key_dicts = [
        {"provider": k.provider, "api_key": k.api_key, "base_url": k.base_url, "is_active": k.is_active}
        for k in keys
    ]
    mgr = get_openclaw_manager()
    await asyncio.to_thread(
        mgr.hot_update_secret,
        instance_id=str(inst.id),
        namespace=inst.namespace,
        gateway_token=inst.gateway_token or "",
        model_keys=key_dicts,
    )


# ====================================================================
#  通道配置管理
# ====================================================================

@router.get("/instances/{instance_id}/channels", response_model=list[ChannelResponse])
async def list_channels(
    instance_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取实例的通道列表"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(Channel).where(Channel.instance_id == inst.id).order_by(Channel.created_at.desc())
    )
    return result.scalars().all()


@router.post("/instances/{instance_id}/channels", response_model=ChannelResponse)
async def add_channel(
    instance_id: UUID,
    req: ChannelCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """添加消息通道"""
    inst = await _get_instance_or_404(instance_id, current_user, db)

    ch = Channel(
        instance_id=inst.id,
        type=req.type,
        name=req.name,
        config=req.config,
    )
    db.add(ch)
    await db.flush()

    await _sync_config(inst, db)
    await db.commit()
    await db.refresh(ch)
    return ch


@router.put("/instances/{instance_id}/channels/{chan_id}", response_model=ChannelResponse)
async def update_channel(
    instance_id: UUID,
    chan_id: UUID,
    req: ChannelUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新通道配置"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(Channel).where(Channel.id == chan_id, Channel.instance_id == inst.id)
    )
    ch = result.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="通道不存在")

    if req.name is not None:
        ch.name = req.name
    if req.config is not None:
        ch.config = req.config
    if req.is_active is not None:
        ch.is_active = req.is_active

    await _sync_config(inst, db)
    await db.commit()
    await db.refresh(ch)
    return ch


@router.delete("/instances/{instance_id}/channels/{chan_id}")
async def delete_channel(
    instance_id: UUID,
    chan_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除通道"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(Channel).where(Channel.id == chan_id, Channel.instance_id == inst.id)
    )
    ch = result.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="通道不存在")

    await db.delete(ch)
    await _sync_config(inst, db)
    await db.commit()
    return {"detail": "通道已删除"}


async def _sync_config(inst: OpenClawInstance, db: AsyncSession):
    """同步通道+Skills 配置到 K8s ConfigMap"""
    ch_result = await db.execute(
        select(Channel).where(Channel.instance_id == inst.id)
    )
    channels = ch_result.scalars().all()

    sk_result = await db.execute(
        select(OpenClawSkill).where(
            OpenClawSkill.instance_id == inst.id,
            OpenClawSkill.status == "installed",
        )
    )
    skills = sk_result.scalars().all()

    ch_dicts = [
        {"type": c.type, "name": c.name, "config": c.config, "is_active": c.is_active}
        for c in channels
    ]
    skill_names = [s.name for s in skills]

    mgr = get_openclaw_manager()
    await asyncio.to_thread(
        mgr.hot_update_config,
        instance_id=str(inst.id),
        namespace=inst.namespace,
        channels=ch_dicts,
        skills=skill_names,
        port=inst.port or 18789,
    )


# ====================================================================
#  Skills 管理（异步执行）
# ====================================================================

@router.get("/instances/{instance_id}/skills", response_model=list[SkillResponse])
async def list_skills(
    instance_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取实例的已安装技能列表"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(OpenClawSkill).where(OpenClawSkill.instance_id == inst.id).order_by(OpenClawSkill.created_at.desc())
    )
    return result.scalars().all()


@router.post("/instances/{instance_id}/skills", response_model=SkillResponse)
async def install_skill(
    instance_id: UUID,
    req: SkillInstall,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """安装 Skill（异步，通过 ARQ 后台执行）"""
    inst = await _get_instance_or_404(instance_id, current_user, db)

    skill = OpenClawSkill(
        instance_id=inst.id,
        name=req.name,
        description=req.description,
        version=req.version,
        status="installing",
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)

    # 入队 ARQ 异步任务
    try:
        await enqueue_task(
            "openclaw_skill_manage",
            str(inst.id), req.name, "install",
        )
    except Exception:
        pass  # Redis 不可用时跳过，状态会在下次同步时更新

    return skill


@router.delete("/instances/{instance_id}/skills/{skill_name}")
async def uninstall_skill(
    instance_id: UUID,
    skill_name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """卸载 Skill（异步）"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(OpenClawSkill).where(
            OpenClawSkill.instance_id == inst.id,
            OpenClawSkill.name == skill_name,
        )
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="技能不存在")

    skill.status = "uninstalling"
    await db.commit()

    try:
        await enqueue_task(
            "openclaw_skill_manage",
            str(inst.id), skill_name, "uninstall",
        )
    except Exception:
        pass

    return {"detail": f"技能 {skill_name} 正在卸载"}


@router.put("/instances/{instance_id}/skills/{skill_id}", response_model=SkillResponse)
async def update_skill(
    instance_id: UUID,
    skill_id: UUID,
    req: SkillUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新技能（版本升级、修改描述）"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(OpenClawSkill).where(
            OpenClawSkill.id == skill_id,
            OpenClawSkill.instance_id == inst.id,
        )
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="技能不存在")

    version_changed = False
    if req.description is not None:
        skill.description = req.description
    if req.version is not None and req.version != skill.version:
        skill.version = req.version
        version_changed = True

    if version_changed:
        skill.status = "installing"
        await db.commit()
        try:
            await enqueue_task(
                "openclaw_skill_manage",
                str(inst.id), skill.name, "install",
            )
        except Exception:
            pass
    else:
        await db.commit()

    await db.refresh(skill)
    return skill


# ====================================================================
#  监控查询
# ====================================================================

@router.get("/instances/{instance_id}/monitor/models", response_model=list[MonitorModelResponse])
async def monitor_models(
    instance_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询大模型密钥监控状态"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(ModelKey).where(ModelKey.instance_id == inst.id)
    )
    keys = result.scalars().all()
    return [
        MonitorModelResponse(
            key_id=k.id,
            provider=k.provider,
            alias=k.alias,
            check_status=k.check_status or "unknown",
            balance=k.balance,
            tokens_used=k.tokens_used or 0,
            last_check_at=k.last_check_at,
        )
        for k in keys
    ]


@router.get("/instances/{instance_id}/monitor/channels", response_model=list[MonitorChannelResponse])
async def monitor_channels(
    instance_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询通道在线状态"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    result = await db.execute(
        select(Channel).where(Channel.instance_id == inst.id)
    )
    channels = result.scalars().all()
    return [
        MonitorChannelResponse(
            channel_id=c.id,
            type=c.type,
            name=c.name,
            online_status=c.online_status or "unknown",
            last_check_at=c.last_check_at,
        )
        for c in channels
    ]


@router.get("/instances/{instance_id}/monitor/status", response_model=MonitorStatusResponse)
async def monitor_status(
    instance_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询实例整体运行状态（含 Gateway 版本、会话数等）"""
    inst = await _get_instance_or_404(instance_id, current_user, db)

    # 统计子资源
    mk_result = await db.execute(select(ModelKey).where(ModelKey.instance_id == inst.id))
    keys = mk_result.scalars().all()
    ch_result = await db.execute(select(Channel).where(Channel.instance_id == inst.id))
    channels = ch_result.scalars().all()
    sk_result = await db.execute(
        select(OpenClawSkill).where(OpenClawSkill.instance_id == inst.id, OpenClawSkill.status == "installed")
    )
    skills = sk_result.scalars().all()

    resp = MonitorStatusResponse(
        instance_id=inst.id,
        status=inst.status,
        internal_ip=inst.internal_ip,
        port=inst.port,
        model_keys_total=len(keys),
        model_keys_ok=sum(1 for k in keys if k.check_status == "ok"),
        channels_total=len(channels),
        channels_online=sum(1 for c in channels if c.online_status == "online"),
        skills_installed=len(skills),
        cpu_cores=inst.cpu_cores,
        memory_gb=inst.memory_gb,
    )

    # 尝试从运行中的实例获取实时数据
    if inst.status == "running" and inst.service_name and inst.namespace:
        try:
            url = build_openclaw_url(inst.service_name, inst.namespace, inst.port)
            client = OpenClawClient(url, inst.gateway_token or "")
            status = await client.get_status()
            if status:
                resp.gateway_version = status.get("version")
                resp.uptime = status.get("uptime")
                resp.session_count = status.get("sessions")
                resp.health = True
                resp.ready = True
        except Exception:
            pass
    elif inst.status == "running":
        resp.health = True

    # ── K8s Metrics: CPU / 内存监控 ──
    if inst.status in ("running", "creating", "starting"):
        try:
            k8s = get_k8s_client()
            if k8s.is_connected:
                inst_ns = inst.namespace or PodManager.user_namespace(str(inst.user_id))
                pod_metrics = await asyncio.to_thread(
                    k8s.list_pod_metrics, namespace=inst_ns
                )
                dep_name = inst.deployment_name or f"oc-{str(inst.id)[:8]}"
                prefix = dep_name
                for pm in pod_metrics:
                    if pm["name"].startswith(prefix):
                        resp.cpu_usage_millicores = pm["cpu_usage_millicores"]
                        resp.memory_usage_bytes = pm["memory_usage_bytes"]
                        break
        except Exception:
            pass

    return resp


# ====================================================================
#  重启 & 日志
# ====================================================================

@router.post("/instances/{instance_id}/restart")
async def restart_instance(
    instance_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重启实例（先停后启）"""
    inst = await _get_instance_or_404(instance_id, current_user, db)
    if inst.status != "running":
        raise HTTPException(status_code=400, detail=f"当前状态 {inst.status} 不可重启")

    mgr = get_openclaw_manager()
    ok = await asyncio.to_thread(mgr.stop_instance, str(inst.id), inst.namespace)
    if not ok:
        raise HTTPException(status_code=500, detail="停止失败")

    ok = await asyncio.to_thread(mgr.start_instance, str(inst.id), inst.namespace)
    if not ok:
        inst.status = "error"
        await db.commit()
        raise HTTPException(status_code=500, detail="重启失败")

    inst.status = "creating"
    inst.started_at = datetime.utcnow()
    await db.commit()
    return {"detail": "重启中"}


@router.delete("/instances/{instance_id}/force", summary="强制删除 OpenClaw 实例")
async def force_delete_instance(
    instance_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """强制删除：无论当前状态，直接清理 K8s 资源并标记为已删除"""
    return await _do_force_delete(instance_id, current_user, db, request)


@router.post("/instances/{instance_id}/force", summary="强制删除 OpenClaw 实例（POST 兼容入口）")
async def force_delete_instance_post(
    instance_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """与 DELETE /force 等价，供不支持 DELETE 的反向代理环境使用"""
    return await _do_force_delete(instance_id, current_user, db, request)


async def _do_force_delete(instance_id: UUID, current_user: User, db: AsyncSession, request: Request = None):
    """强制删除核心逻辑：先 DB 标记 released，再后台清理 K8s"""
    result = await db.execute(
        select(OpenClawInstance)
        .where(OpenClawInstance.id == instance_id, OpenClawInstance.user_id == current_user.id)
    )
    inst = result.scalar_one_or_none()
    if not inst:
        raise HTTPException(status_code=404, detail="实例不存在")

    # 即时结算
    try:
        from app.api.v1.billing import settle_instance_billing
        user_result = await db.execute(select(User).where(User.id == current_user.id))
        user_obj = user_result.scalar_one()
        await settle_instance_billing(inst, user_obj, db, "openclaw", "强制删除结算")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"OpenClaw 强制删除结算失败: {e}")

    inst.status = "released"
    await db.commit()

    # 后台清理 K8s 资源
    try:
        mgr = get_openclaw_manager()
        await asyncio.to_thread(
            mgr.release_instance,
            instance_id=str(inst.id),
            namespace=inst.namespace,
            node_type=inst.node_type or "center",
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"OpenClaw 强制删除 K8s 清理异常（已忽略）: {e}")

    # 审计日志
    try:
        from app.api.v1.audit_log import create_audit_log
        from app.models import AuditAction, AuditResourceType
        from app.api.v1.audit_log import get_client_ip
        await create_audit_log(
            db, current_user.id, AuditAction.DELETE, AuditResourceType.OPENCLAW,
            resource_id=str(inst.id), resource_name=inst.name,
            detail="强制删除",
            ip_address=get_client_ip(request) if request else None,
        )
        await db.commit()
    except Exception:
        pass

    return {"detail": "实例已强制删除"}


@router.get("/instances/{instance_id}/logs")
async def get_instance_logs(
    instance_id: UUID,
    tail: int = 200,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取 OpenClaw 实例 Pod 日志"""
    import logging
    logger = logging.getLogger(__name__)

    inst = await _get_instance_or_404(instance_id, current_user, db)

    from app.services.k8s_client import get_k8s_client
    k8s = get_k8s_client()
    # OpenClaw Pod 标签为 openclaw-instance={instance_id}（非 instance-id）
    try:
        pods = await asyncio.to_thread(
            k8s.list_pods,
            inst.namespace,
            f"openclaw-instance={inst.id}",
        )
    except Exception as e:
        logger.warning(f"OpenClaw 日志: list_pods 失败 ns={inst.namespace} id={inst.id}: {e}")
        return {"logs": f"(获取 Pod 列表失败: {e})"}

    if not pods:
        return {"logs": f"(未找到运行中的 Pod，namespace={inst.namespace}, label=openclaw-instance={inst.id})"}

    pod_name = pods[0]["name"]
    pod_status = pods[0].get("effective_status", pods[0].get("status", "unknown"))
    logger.info(f"OpenClaw 日志: pod={pod_name} status={pod_status} ns={inst.namespace}")

    try:
        # 显式指定容器名 gateway（Pod 含 init-config 初始化容器，需明确指定主容器）
        logs = await asyncio.to_thread(
            k8s.get_pod_logs, pod_name, inst.namespace, tail, "gateway",
        )
    except Exception as e:
        logger.warning(f"OpenClaw 日志: get_pod_logs 失败 pod={pod_name}: {e}")
        return {"logs": f"(获取日志失败: {e})"}

    if logs is None:
        return {"logs": f"(Pod {pod_name} 日志为空，容器可能尚未就绪，当前状态: {pod_status})"}

    return {"logs": logs}
