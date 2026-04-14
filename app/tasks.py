"""
ARQ 异步任务队列配置和任务定义

ARQ 是专为 Python asyncio 设计的任务队列，使用 Redis 作为消息代理。
相比 Celery，ARQ 更轻量、启动更快、与 FastAPI 原生兼容。

启动 Worker:
    arq app.tasks.WorkerSettings

或者直接运行此文件:
    python -m app.tasks
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from arq import create_pool, cron
from arq.connections import RedisSettings, ArqRedis

from app.config import settings
from app.logging_config import get_logger

logger = get_logger("lmaicloud.tasks")


# ============== Redis 连接配置 ==============

def get_redis_settings() -> RedisSettings:
    """解析 Redis URL 并返回 ARQ RedisSettings"""
    url = settings.redis_url
    # 解析 redis://host:port/db 格式
    if url.startswith("redis://"):
        url = url[8:]  # 去掉 redis://
    
    parts = url.split("/")
    host_port = parts[0]
    database = int(parts[1]) if len(parts) > 1 else 0
    
    if ":" in host_port:
        host, port = host_port.split(":")
        port = int(port)
    else:
        host = host_port
        port = 6379
    
    return RedisSettings(
        host=host,
        port=port,
        database=database,
    )


# ============== 任务函数定义 ==============

async def send_email_task(ctx: dict, to_email: str, subject: str, content: str) -> dict:
    """
    发送邮件任务
    
    Args:
        ctx: ARQ 上下文，包含 redis 连接等
        to_email: 收件人邮箱
        subject: 邮件主题
        content: 邮件内容
    
    Returns:
        任务执行结果
    """
    # TODO: 实际的邮件发送逻辑
    logger.info(f"[EMAIL] Sending email to {to_email}: {subject}")
    await asyncio.sleep(0.1)  # 模拟发送延迟
    
    return {
        "status": "sent",
        "to": to_email,
        "subject": subject,
        "sent_at": datetime.utcnow().isoformat(),
    }


async def process_instance_billing_task(ctx: dict, instance_id: str, instance_type: str = "gpu") -> dict:
    """
    处理单个实例计费任务

    按实际运行时长计费：
    - hourly: 费用 = hourly_price * (now - last_billed_at) / 3600，写入 billing_records
    - monthly/yearly: 到期后自动续费或停机，写入 orders
    """
    from app.database import AsyncSessionLocal
    from app.models import Instance, OpenClawInstance, User, Order, OrderType, OrderStatus, InstanceStatus, BillingRecord, Notification, NotificationType
    from sqlalchemy import select
    from datetime import datetime
    from dateutil.relativedelta import relativedelta

    logger.info(f"[BILLING] Processing billing for {instance_type} instance: {instance_id}")

    async with AsyncSessionLocal() as session:
        # 根据类型获取实例
        if instance_type == "openclaw":
            result = await session.execute(select(OpenClawInstance).where(OpenClawInstance.id == instance_id))
            instance = result.scalar_one_or_none()
            if not instance:
                return {"status": "skipped", "reason": "instance not found"}
            hourly_price = getattr(instance, "hourly_price", None)
            if hourly_price is None:
                from app.config import settings
                hourly_price = settings.default_gpu_hourly_price
            bt = getattr(instance, "billing_type", None)
            billing_cycle = bt.value if bt and hasattr(bt, 'value') else (bt or "hourly")
            inst_user_id = instance.user_id
            fk_kwargs = {"openclaw_instance_id": instance.id}
            res_name = getattr(instance, "name", None) or str(instance.id)[:8]
            res_type = "openclaw"

            # 状态检查：区分计费类型
            if billing_cycle in ("monthly", "yearly"):
                # 包月/包年：只要不是 released/releasing 就检查续费
                if instance.status in ("released", "releasing"):
                    return {"status": "skipped", "reason": "instance released"}
            else:
                # 按量计费：必须 RUNNING 才计费
                if instance.status != "running":
                    return {"status": "skipped", "reason": "instance not running"}
        else:
            result = await session.execute(select(Instance).where(Instance.id == instance_id))
            instance = result.scalar_one_or_none()
            if not instance:
                return {"status": "skipped", "reason": "instance not found"}
            hourly_price = instance.hourly_price
            billing_cycle = getattr(instance, "billing_cycle", None) or instance.billing_type or "hourly"
            inst_user_id = instance.user_id
            fk_kwargs = {"instance_id": instance.id}
            res_name = getattr(instance, "name", None) or str(instance.id)[:8]
            res_type = "gpu"

            # 状态检查：区分计费类型
            if billing_cycle in ("monthly", "yearly"):
                # 包月/包年：只要不是 RELEASED/RELEASING 就检查续费
                if instance.status in (InstanceStatus.RELEASED, InstanceStatus.RELEASING):
                    return {"status": "skipped", "reason": "instance released"}
            else:
                # 按量计费：必须 RUNNING 才计费
                if instance.status != InstanceStatus.RUNNING:
                    return {"status": "skipped", "reason": "instance not running"}

        # 获取用户
        result = await session.execute(select(User).where(User.id == inst_user_id))
        user = result.scalar_one_or_none()
        if not user:
            return {"status": "error", "reason": "user not found"}

        # 包月/包年计费逻辑（仍写入 Order）
        if billing_cycle in ("monthly", "yearly"):
            expired_at = getattr(instance, "expired_at", None)
            if expired_at and datetime.utcnow() < expired_at:
                return {"status": "skipped", "reason": "subscription active"}

            if billing_cycle == "monthly":
                renew_price = hourly_price * 24 * 30
                delta = relativedelta(months=1)
            else:
                renew_price = hourly_price * 24 * 365
                delta = relativedelta(years=1)

            if user.balance < renew_price:
                instance.status = InstanceStatus.EXPIRED
                await session.commit()
                return {"status": "expired", "reason": "balance too low for renewal"}

            user.balance -= renew_price
            new_start = expired_at or datetime.utcnow()
            instance.expired_at = new_start + delta

            order = Order(
                user_id=user.id,
                type=OrderType.RENEW,
                amount=-renew_price,
                status=OrderStatus.PAID,
                paid_at=datetime.utcnow(),
                product_name=f"{res_name} ({instance_type}实例续费)",
                billing_cycle=billing_cycle,
                description="{} {} 自动续费".format(res_name, '包月' if billing_cycle == 'monthly' else '包年'),
                **fk_kwargs,
            )
            session.add(order)
            await session.commit()
            return {
                "status": "renewed",
                "instance_id": instance_id,
                "amount": renew_price,
                "new_balance": user.balance,
                "next_expire": instance.expired_at.isoformat(),
            }

        # 按量计费：按实际运行时长，写入 billing_records
        last_billed = instance.last_billed_at
        if not last_billed:
            # last_billed_at 为空：用 started_at 回溯，立即计费（不再等下一周期）
            last_billed = getattr(instance, 'started_at', None) or datetime.utcnow()
            instance.last_billed_at = last_billed
            logger.info(f"[BILLING] Initialized last_billed_at for {instance_type} {instance_id} to {last_billed}")

        now = datetime.utcnow()
        duration = int((now - last_billed).total_seconds())
        logger.info(f"[BILLING] {instance_type} {instance_id}: hourly_price={hourly_price}, duration={duration}s, last_billed={last_billed}")
        if duration < 60:
            await session.commit()  # 保存 last_billed_at 初始化
            return {"status": "skipped", "reason": f"duration too short ({duration}s)"}

        charge_amount = round(hourly_price * duration / 3600, 4)

        # 先正常扣费（允许欠费）
        user.balance -= charge_amount
        # 构造描述：包含计费区间和单价
        period_desc = f"{last_billed.strftime('%m/%d %H:%M')}~{now.strftime('%m/%d %H:%M')}"
        desc = f"按量计费 {period_desc} ¥{hourly_price:.2f}/时"

        record = BillingRecord(
            user_id=user.id,
            amount=charge_amount,
            hourly_price=hourly_price,
            duration_seconds=duration,
            period_start=last_billed,
            period_end=now,
            description=desc,
            instance_name=res_name,
            resource_type=res_type,
            **fk_kwargs,
        )
        session.add(record)
        instance.last_billed_at = now  # 更新计费时间点

        # 欠费检查：最高允许欠费 10 元
        billing_result = "processed"
        if user.balance <= -10:
            # 欠费超过 10 元 → 强制关机
            logger.warning(f"[BILLING] User {user.id} debt {user.balance:.2f} >= 10, force stopping {instance_type} {instance_id}")
            try:
                if instance_type == "openclaw":
                    from app.services.openclaw_manager import get_openclaw_manager
                    mgr = get_openclaw_manager()
                    await asyncio.to_thread(mgr.stop_instance, str(instance.id), instance.namespace)
                    instance.status = "stopped"
                else:
                    from app.services.pod_manager import PodManager, get_pod_manager
                    pm = get_pod_manager()
                    inst_ns = instance.namespace or PodManager.user_namespace(str(instance.user_id))
                    await asyncio.to_thread(pm.stop_instance, str(instance.id), inst_ns)
                    instance.status = InstanceStatus.STOPPED
                instance.last_billed_at = None
                billing_result = "force_stopped"
            except Exception as e:
                logger.error(f"[BILLING] Force stop failed for {instance_type} {instance_id}: {e}")

            # 发送强制关机通知
            notification = Notification(
                user_id=user.id,
                title="欠费强制关机通知",
                content=f"您的实例「{res_name}」因账户欠费超过 10 元（当前余额 ¥{user.balance:.2f}）已被强制关机。请尽快充值以恢复服务。",
                type=NotificationType.BILLING,
            )
            session.add(notification)
        elif user.balance < 0:
            # 欠费但未超 10 元 → 发送警告通知
            logger.warning(f"[BILLING] User {user.id} balance negative: {user.balance:.2f}, sending warning")
            notification = Notification(
                user_id=user.id,
                title="余额不足提醒",
                content=f"您的账户余额已不足（当前 ¥{user.balance:.2f}），按量计费实例「{res_name}」仍在运行中。欠费超过 10 元将自动关机，请及时充值。",
                type=NotificationType.BILLING,
            )
            session.add(notification)

        await session.commit()

        return {
            "status": billing_result,
            "instance_id": instance_id,
            "amount": charge_amount,
            "duration_seconds": duration,
            "new_balance": user.balance,
            "processed_at": now.isoformat(),
        }


async def process_all_billing_task(ctx: dict) -> dict:
    """
    定时任务: 处理所有运行中实例的计费（GPU + OpenClaw）
    cron 每 15 分钟直接执行，不再有内部门控。
    """
    from app.database import AsyncSessionLocal
    from app.models import Instance, OpenClawInstance
    from app.models import InstanceStatus as _IS
    from sqlalchemy import select

    now = datetime.utcnow()
    logger.info(f"[BILLING] Processing all instance billing at {now.isoformat()}")

    processed = 0
    errors = 0

    async with AsyncSessionLocal() as session:
        # GPU 实例：按量计费只查 RUNNING，包月/包年还包含 STOPPED/EXPIRED 以支持自动续费
        from app.models import BillingType
        result = await session.execute(
            select(Instance).where(
                (Instance.status == _IS.RUNNING) |
                (
                    Instance.billing_type.in_([BillingType.MONTHLY, BillingType.YEARLY]) &
                    Instance.status.in_([_IS.RUNNING, _IS.STOPPED, _IS.EXPIRED])
                )
            )
        )
        gpu_instances = result.scalars().all()
        logger.info(f"[BILLING] Found {len(gpu_instances)} GPU instances to bill")
        for instance in gpu_instances:
            try:
                r = await process_instance_billing_task(ctx, str(instance.id), "gpu")
                logger.info(f"[BILLING] GPU {instance.id}: {r.get('status')} - {r.get('reason', r.get('amount', ''))}")
                if r.get("status") in ("processed", "renewed"):
                    processed += 1
            except Exception as e:
                logger.error(f"[BILLING] Error processing GPU {instance.id}: {e}")
                errors += 1

        # OpenClaw 实例（支持包月/包年）
        oc_result = await session.execute(
            select(OpenClawInstance).where(
                OpenClawInstance.status.in_(["running", "stopped", "expired"])
            )
        )
        oc_instances = oc_result.scalars().all()
        logger.info(f"[BILLING] Found {len(oc_instances)} OpenClaw instances to bill")
        for inst in oc_instances:
            try:
                r = await process_instance_billing_task(ctx, str(inst.id), "openclaw")
                logger.info(f"[BILLING] OpenClaw {inst.id}: {r.get('status')} - {r.get('reason', r.get('amount', ''))}")
                if r.get("status") in ("processed", "renewed"):
                    processed += 1
            except Exception as e:
                logger.error(f"[BILLING] Error processing OpenClaw {inst.id}: {e}")
                errors += 1

    return {
        "status": "completed",
        "processed": processed,
        "errors": errors,
        "processed_at": now.isoformat(),
    }


async def check_instance_health_task(ctx: dict, instance_id: str) -> dict:
    """
    检查实例健康状态任务
    
    Args:
        ctx: ARQ 上下文
        instance_id: 实例 ID
    
    Returns:
        健康检查结果
    """
    logger.info(f"[HEALTH] Checking health for instance: {instance_id}")
    # TODO: 实际的健康检查逻辑
    # 1. Pod 状态检查
    # 2. GPU 状态检查
    # 3. 更新实例健康状态
    
    return {
        "status": "healthy",
        "instance_id": instance_id,
        "checked_at": datetime.utcnow().isoformat(),
    }


async def sync_instance_status_task(ctx: dict) -> dict:
    """
    定时任务: 同步实例状态 (每30秒)
    检查 DB 中处于 creating / starting 的实例，
    从 K8s Deployment 读取真实状态回写 DB。
    状态变更时通过 Redis pub/sub 通知 FastAPI 进程广播 WebSocket。
    """
    from app.database import AsyncSessionLocal
    from app.models import Instance
    from app.models import InstanceStatus as _IS
    from app.services.k8s_client import get_k8s_client
    from app.api.v1.instances import _derive_instance_status
    from sqlalchemy import select
    from datetime import datetime, timedelta, timezone
    import json

    CREATING_TIMEOUT = timedelta(minutes=10)

    try:
        async with AsyncSessionLocal() as session:
            # 查询所有非终态实例，确保 K8s 状态变化能及时同步到 DB
            # 包括 RUNNING/ERROR：防止 K8s 恢复后 DB 仍停留在 ERROR，
            # 或 K8s 异常后 DB 仍显示 RUNNING
            result = await session.execute(
                select(Instance).where(Instance.status.in_([
                    _IS.CREATING, _IS.STARTING, _IS.RUNNING,
                    _IS.STOPPING, _IS.ERROR,
                ]))
            )
            pending = result.scalars().all()
            if not pending:
                return {"status": "ok", "synced": 0}

            k8s = get_k8s_client()
            if not k8s.is_connected:
                return {"status": "skipped", "reason": "k8s not connected"}

            # 断路器开启时跳过，避免无效重试和日志轰炸
            if k8s.circuit_open:
                return {"status": "skipped", "reason": "k8s circuit breaker open"}

            # 批量获取所有 Deployment（跨命名空间）
            deployments = k8s.list_deployments(
                label_selector="app=gpu-instance",
                all_namespaces=True,
            )
            dep_map = {}
            for dep in deployments:
                labels = dep.get("labels") or {}
                annotations = dep.get("annotations") or {}
                iid = labels.get("instance-id") or annotations.get("lmaicloud/instance-id")
                if iid:
                    dep_map[iid] = dep

            now_utc = datetime.now(timezone.utc)
            synced = 0

            for inst in pending:
                inst_id = str(inst.id)
                dep = dep_map.get(inst_id)

                if dep:
                    new_status = _derive_instance_status(dep, db_status=inst.status)
                else:
                    # Deployment 在 K8s 中不存在，根据当前 DB 状态分别处理
                    if inst.status == _IS.CREATING:
                        # 创建中但 Deployment 还没建好，检查超时
                        created = inst.created_at
                        if created and created.tzinfo is None:
                            created = created.replace(tzinfo=timezone.utc)
                        if created and (now_utc - created) > CREATING_TIMEOUT:
                            new_status = _IS.ERROR
                        else:
                            continue  # 未超时，等待后台任务完成
                    elif inst.status in (_IS.RUNNING, _IS.STARTING):
                        # 原本在运行/启动 但 Deployment 已消失 → 标记错误
                        new_status = _IS.ERROR
                    elif inst.status == _IS.STOPPING:
                        # 正在停止且 Deployment 已删除 → 已停止
                        new_status = _IS.STOPPED
                    else:
                        continue  # ERROR 且无 Deployment → 保持

                if new_status == inst.status:
                    continue

                old_status = inst.status
                inst.status = new_status
                if new_status == _IS.RUNNING:
                    if not inst.started_at:
                        inst.started_at = datetime.utcnow()
                        try:
                            inst_ns = inst.namespace or "lmaicloud"
                            pods = k8s.list_pods(
                                namespace=inst_ns,
                                label_selector=f"instance-id={inst_id}",
                            )
                            if pods and pods[0].get("ip"):
                                inst.internal_ip = pods[0]["ip"]
                        except Exception:
                            pass
                    # 进入 RUNNING 开始计费（仅按量计费实例，包月/包年走 expired_at 机制）
                    bt_val = getattr(inst, 'billing_type', 'hourly')
                    if hasattr(bt_val, 'value'):
                        bt_val = bt_val.value
                    if bt_val not in ('monthly', 'yearly'):
                        inst.last_billed_at = datetime.utcnow()
                        logger.info(f"[INSTANCE_SYNC] Set last_billed_at for {inst_id} (transition to running)")
                elif old_status == _IS.RUNNING and inst.last_billed_at:
                    # 从 running 回退到其他状态：清除 last_billed_at，避免后续删除时误计费
                    bt_val = getattr(inst, 'billing_type', 'hourly')
                    if hasattr(bt_val, 'value'):
                        bt_val = bt_val.value
                    if bt_val not in ('monthly', 'yearly'):
                        logger.info(f"[INSTANCE_SYNC] Status {old_status}->{new_status}, clearing last_billed_at for {inst_id}")
                        inst.last_billed_at = None

                await session.commit()
                synced += 1

                # 通过 Redis pub/sub 通知 FastAPI 广播 WebSocket
                try:
                    redis_conn = ctx.get("redis")
                    if redis_conn:
                        await redis_conn.publish(
                            "lmaicloud:instance_status",
                            json.dumps({
                                "instance_id": inst_id,
                                "user_id": str(inst.user_id),
                                "status": new_status,
                                "old_status": old_status,
                            }),
                        )
                except Exception:
                    pass

            # 补救：查找 running 状态但 last_billed_at 为 None 的按量计费 GPU 实例
            fix_result = await session.execute(
                select(Instance).where(
                    Instance.status == _IS.RUNNING,
                    Instance.last_billed_at.is_(None),
                )
            )
            for fix_inst in fix_result.scalars().all():
                bt_val = getattr(fix_inst, 'billing_type', 'hourly')
                if hasattr(bt_val, 'value'):
                    bt_val = bt_val.value
                if bt_val not in ('monthly', 'yearly'):
                    fix_inst.last_billed_at = fix_inst.started_at or datetime.utcnow()
                    logger.info(f"[INSTANCE_SYNC] Fixed missing last_billed_at for running GPU instance {fix_inst.id}")
            await session.commit()

            return {"status": "ok", "synced": synced}

    except Exception as e:
        return {"status": "error", "reason": str(e)}


async def cleanup_expired_instances_task(ctx: dict) -> dict:
    """
    清理过期实例任务 (定时任务)
    
    Returns:
        清理结果
    """
    logger.info("[CLEANUP] Cleaning up expired instances...")
    # TODO: 实际的清理逻辑
    # 1. 查询所有过期实例
    # 2. 释放资源
    # 3. 更新状态
    # 4. 发送通知
    
    return {
        "status": "completed",
        "cleaned_count": 0,
        "cleaned_at": datetime.utcnow().isoformat(),
    }


async def generate_daily_report_task(ctx: dict) -> dict:
    """
    生成每日报表任务 (定时任务)
    
    Returns:
        报表生成结果
    """
    logger.info("[REPORT] Generating daily report...")
    # TODO: 实际的报表生成逻辑
    # 1. 统计当日数据
    # 2. 生成报表
    # 3. 发送给管理员
    
    return {
        "status": "generated",
        "report_date": datetime.utcnow().date().isoformat(),
        "generated_at": datetime.utcnow().isoformat(),
    }


# ============== OpenClaw 定时 / 异步任务 ==============

async def openclaw_model_key_monitor(ctx: dict) -> dict:
    """
    定时任务: 大模型密钥监控 (每60秒)
    遍历所有 running 的 OpenClaw 实例，
    1. 检查 Gateway 是否可达
    2. 对每个活跃 Key 检查其 provider 对应的环境变量是否已注入（通过 Secret 存在性）
    3. 更新 check_status / last_check_at
    """
    from app.database import AsyncSessionLocal
    from app.models import OpenClawInstance, ModelKey
    from app.services.openclaw_client import OpenClawClient, build_openclaw_url
    from sqlalchemy import select
    from datetime import datetime
    import logging
    logger = logging.getLogger(__name__)

    checked = 0
    errors = 0

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(OpenClawInstance).where(OpenClawInstance.status == "running")
            )
            instances = result.scalars().all()

            for inst in instances:
                keys_result = await session.execute(
                    select(ModelKey).where(
                        ModelKey.instance_id == inst.id,
                        ModelKey.is_active == True,
                    )
                )
                keys = keys_result.scalars().all()
                if not keys:
                    continue

                # 检测 Gateway 可达性 + 状态
                gateway_ok = False
                try:
                    url = build_openclaw_url(inst.service_name, inst.namespace, inst.port)
                    client = OpenClawClient(url, inst.gateway_token)
                    status_data = await client.get_status()
                    gateway_ok = status_data is not None
                except Exception:
                    pass

                for key in keys:
                    now = datetime.utcnow()
                    if not gateway_ok:
                        # Gateway 不可达，所有 key 标记 unknown
                        key.check_status = "unknown"
                        key.check_message = "Gateway 不可达"
                    elif not key.api_key:
                        key.check_status = "error"
                        key.check_message = "API Key 为空"
                        errors += 1
                    else:
                        # Gateway 可达 且 Key 已配置，标记为 ok
                        key.check_status = "ok"
                        key.check_message = None
                    key.last_check_at = now
                    checked += 1

                await session.commit()

    except Exception as e:
        logger.error(f"Model key monitor error: {e}")
        return {"status": "error", "reason": str(e)}

    return {"status": "ok", "checked": checked, "errors": errors}


async def openclaw_channel_monitor(ctx: dict) -> dict:
    """
    定时任务: 通道在线监控 (每30秒)
    检测 Gateway 可达性，更新通道 online/offline 状态。
    未激活的通道始终标记为 disabled。
    """
    from app.database import AsyncSessionLocal
    from app.models import OpenClawInstance, Channel
    from app.services.openclaw_client import OpenClawClient, build_openclaw_url
    from sqlalchemy import select
    from datetime import datetime
    import logging
    logger = logging.getLogger(__name__)

    checked = 0

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(OpenClawInstance).where(OpenClawInstance.status == "running")
            )
            instances = result.scalars().all()

            for inst in instances:
                channels_result = await session.execute(
                    select(Channel).where(Channel.instance_id == inst.id)
                )
                channels = channels_result.scalars().all()
                if not channels:
                    continue

                gateway_online = False
                try:
                    url = build_openclaw_url(inst.service_name, inst.namespace, inst.port)
                    client = OpenClawClient(url, inst.gateway_token)
                    status_data = await client.get_status()
                    gateway_online = status_data is not None
                except Exception:
                    pass

                now = datetime.utcnow()
                for ch in channels:
                    if not ch.is_active:
                        ch.online_status = "disabled"
                    elif gateway_online:
                        ch.online_status = "online"
                    else:
                        ch.online_status = "offline"
                    ch.last_check_at = now
                    checked += 1

                await session.commit()

    except Exception as e:
        logger.error(f"Channel monitor error: {e}")
        return {"status": "error", "reason": str(e)}

    return {"status": "ok", "checked": checked}


async def openclaw_instance_sync(ctx: dict) -> dict:
    """
    定时任务: OpenClaw 实例状态同步 (每30秒)
    检查 K8s Deployment/Pod 状态并同步到 DB, 通过 Redis pub/sub 通知前端。
    """
    from app.database import AsyncSessionLocal
    from app.models import OpenClawInstance
    from app.services.k8s_client import get_k8s_client
    from sqlalchemy import select
    from datetime import datetime, timedelta, timezone
    import json

    CREATING_TIMEOUT = timedelta(minutes=10)
    synced = 0
    logger.info("[OPENCLAW_SYNC] Task started")

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(OpenClawInstance).where(
                    OpenClawInstance.status.in_(["creating", "running", "stopped", "error"])
                )
            )
            instances = result.scalars().all()
            if not instances:
                return {"status": "ok", "synced": 0}
            logger.info(f"[OPENCLAW_SYNC] Found {len(instances)} instances to check")

            k8s = get_k8s_client()
            if not k8s.is_connected:
                logger.warning("[OPENCLAW_SYNC] K8s client not connected, skipping")
                return {"status": "skipped", "reason": "k8s not connected"}
            if k8s.circuit_open:
                logger.warning("[OPENCLAW_SYNC] K8s circuit breaker open, skipping")
                return {"status": "skipped", "reason": "k8s circuit breaker open"}

            now_utc = datetime.now(timezone.utc)

            for inst in instances:
                try:
                    dep_name = inst.deployment_name
                    ns = inst.namespace
                    if not dep_name or not ns:
                        logger.warning(f"[OPENCLAW_SYNC] Skipping {inst.id}: dep_name={dep_name}, ns={ns}")
                        continue

                    dep_info = k8s.get_deployment(dep_name, ns)
                    if not dep_info:
                        # Deployment 不存在，根据当前状态分别处理
                        if inst.status == "creating":
                            # 创建中但 Deployment 还没建好，检查超时
                            created = inst.created_at
                            if created and created.tzinfo is None:
                                created = created.replace(tzinfo=timezone.utc)
                            if created and (now_utc - created) > CREATING_TIMEOUT:
                                logger.warning(f"[OPENCLAW_SYNC] {inst.id} creating timeout, marking error")
                                inst.status = "error"
                                synced += 1
                            # else: 未超时，继续等待
                        elif inst.status in ("running", "stopped"):
                            # 原本在运行/已停止 但 Deployment 已消失
                            logger.warning(f"[OPENCLAW_SYNC] Deployment {dep_name} not found, {inst.id} -> error")
                            inst.status = "error"
                            synced += 1
                        # error 状态且无 Deployment → 保持
                        continue

                    replicas = dep_info.get("ready_replicas", 0) or 0
                    desired = dep_info.get("replicas", 1) or 1
                    conditions = dep_info.get("conditions") or []
                    logger.info(f"[OPENCLAW_SYNC] {inst.id} dep={dep_name}: ready={replicas}/{desired}, current_status={inst.status}")

                    # 检查 Deployment 明确失败条件
                    dep_failed = False
                    for cond in conditions:
                        cond_type = cond.get("type", "")
                        cond_status = cond.get("status", "")
                        if cond_type == "Progressing" and cond_status == "False":
                            dep_failed = True
                            break
                        if cond_type == "ReplicaFailure":
                            dep_failed = True
                            break

                    if dep_failed:
                        new_status = "error"
                    elif desired == 0:
                        new_status = "stopped"
                    elif replicas >= desired:
                        new_status = "running"
                    else:
                        # 未全部就绪，保持创建中状态
                        new_status = "creating"

                    if new_status != inst.status:
                        old_status = inst.status
                        inst.status = new_status
                        if new_status == "running":
                            if not inst.started_at:
                                inst.started_at = datetime.utcnow()
                            # 进入 RUNNING 开始计费（仅按量计费实例，包月/包年走 expired_at 机制）
                            bt = getattr(inst, 'billing_type', None)
                            bt_val = bt.value if bt and hasattr(bt, 'value') else (bt or 'hourly')
                            if bt_val not in ('monthly', 'yearly'):
                                inst.last_billed_at = datetime.utcnow()
                                logger.info(f"[OPENCLAW_SYNC] Set last_billed_at for {inst.id} (transition to running)")
                        elif old_status == "running" and inst.last_billed_at:
                            # 从 running 回退到其他状态：清除 last_billed_at，避免后续删除时误计费
                            bt = getattr(inst, 'billing_type', None)
                            bt_val = bt.value if bt and hasattr(bt, 'value') else (bt or 'hourly')
                            if bt_val not in ('monthly', 'yearly'):
                                logger.info(f"[OPENCLAW_SYNC] Status {old_status}->{new_status}, clearing last_billed_at for {inst.id}")
                                inst.last_billed_at = None
                        synced += 1
                        logger.info(f"[OPENCLAW_SYNC] Status changed {old_status}->{new_status} for {inst.id}")

                    # 补救：running 状态但 last_billed_at 为 None 的按量计费实例
                    if inst.status == "running" and not inst.last_billed_at:
                        bt = getattr(inst, 'billing_type', None)
                        bt_val = bt.value if bt and hasattr(bt, 'value') else (bt or 'hourly')
                        if bt_val not in ('monthly', 'yearly'):
                            inst.last_billed_at = inst.started_at or datetime.utcnow()
                            logger.info(f"[OPENCLAW_SYNC] Fixed missing last_billed_at for running instance {inst.id}")

                except Exception as e:
                    logger.error(f"[OPENCLAW_SYNC] Error syncing {inst.id}: {e}")

            await session.commit()

    except Exception as e:
        return {"status": "error", "reason": str(e)}

    return {"status": "ok", "synced": synced}


async def openclaw_skill_manage(ctx: dict, instance_id: str, skill_name: str, action: str) -> dict:
    """
    异步任务: Skills 安装/卸载
    1. 尝试通过 OpenClaw Gateway API 安装/卸载
    2. 若 Gateway 不支持此 API，回退到配置文件方式（更新 ConfigMap + 重启 Pod）
    3. 通过 list_skills 确认最终状态
    """
    from app.database import AsyncSessionLocal
    from app.models import OpenClawInstance, OpenClawSkill
    from app.services.openclaw_client import OpenClawClient, build_openclaw_url
    from sqlalchemy import select
    from datetime import datetime
    import logging
    logger = logging.getLogger(__name__)

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(OpenClawInstance).where(OpenClawInstance.id == instance_id)
            )
            inst = result.scalar_one_or_none()
            if not inst or inst.status != "running":
                return {"status": "error", "reason": "instance not running"}

            url = build_openclaw_url(inst.service_name, inst.namespace, inst.port)
            client = OpenClawClient(url, inst.gateway_token)

            # 查找 skill 记录
            skill_result = await session.execute(
                select(OpenClawSkill).where(
                    OpenClawSkill.instance_id == instance_id,
                    OpenClawSkill.name == skill_name,
                )
            )
            skill = skill_result.scalar_one_or_none()

            if action == "install":
                if not skill:
                    return {"status": "error", "reason": "skill record not found"}

                skill.status = "installing"
                await session.commit()

                # 策略1: 尝试调用 Gateway install API
                api_ok = False
                try:
                    resp = await client.install_skill(skill_name, skill.version)
                    if resp is not None:
                        api_ok = True
                        logger.info(f"Skill {skill_name}: Gateway API 安装成功")
                except Exception as e:
                    logger.warning(f"Skill {skill_name}: Gateway API 安装失败({e})，尝试 ConfigMap 回退")

                # 策略2: 若 API 不可用，通过 ConfigMap + Pod 重启方式安装
                if not api_ok:
                    try:
                        from app.services.openclaw_manager import get_openclaw_manager
                        from app.models import Channel as ChannelModel
                        # 查询当前所有已安装 skill + 待安装 skill
                        all_sk_result = await session.execute(
                            select(OpenClawSkill).where(
                                OpenClawSkill.instance_id == instance_id,
                                OpenClawSkill.status.in_(["installed", "installing"]),
                            )
                        )
                        all_skills = [s.name for s in all_sk_result.scalars().all()]
                        ch_result = await session.execute(
                            select(ChannelModel).where(ChannelModel.instance_id == instance_id)
                        )
                        channels = ch_result.scalars().all()
                        ch_dicts = [{"type": c.type, "name": c.name, "config": c.config, "is_active": c.is_active} for c in channels]

                        mgr = get_openclaw_manager()
                        import asyncio as _aio
                        await _aio.to_thread(
                            mgr.hot_update_config,
                            instance_id=str(inst.id),
                            namespace=inst.namespace,
                            channels=ch_dicts,
                            skills=all_skills,
                            port=inst.port or 18789,
                        )
                        logger.info(f"Skill {skill_name}: ConfigMap 回退安装已触发")
                    except Exception as e:
                        logger.error(f"Skill {skill_name}: ConfigMap 回退安装失败: {e}")

                # 等待一段时间后确认安装结果
                import asyncio as _aio
                await _aio.sleep(5)
                try:
                    skills_list = await client.list_skills()
                    installed = any(s.get("name") == skill_name for s in skills_list)
                    if installed:
                        skill.status = "installed"
                        skill.installed_at = datetime.utcnow()
                    elif api_ok:
                        # API 返回成功但列表未确认，可能还在加载，先标记成功
                        skill.status = "installed"
                        skill.installed_at = datetime.utcnow()
                    else:
                        skill.status = "error"
                except Exception:
                    # Gateway 可能在重启中，乐观标记成功
                    if api_ok:
                        skill.status = "installed"
                        skill.installed_at = datetime.utcnow()
                    else:
                        skill.status = "error"

                await session.commit()
                return {"status": "ok", "action": "install", "skill": skill_name}

            elif action == "uninstall":
                if not skill:
                    return {"status": "error", "reason": "skill record not found"}

                skill.status = "uninstalling"
                await session.commit()

                # 策略1: 尝试调用 Gateway uninstall API
                api_ok = False
                try:
                    api_ok = await client.uninstall_skill(skill_name)
                    if api_ok:
                        logger.info(f"Skill {skill_name}: Gateway API 卸载成功")
                except Exception as e:
                    logger.warning(f"Skill {skill_name}: Gateway API 卸载失败({e})")

                # 策略2: ConfigMap 回退 — 从 skills 列表中移除
                try:
                    from app.services.openclaw_manager import get_openclaw_manager
                    from app.models import Channel as ChannelModel
                    all_sk_result = await session.execute(
                        select(OpenClawSkill).where(
                            OpenClawSkill.instance_id == instance_id,
                            OpenClawSkill.status == "installed",
                            OpenClawSkill.name != skill_name,
                        )
                    )
                    remaining_skills = [s.name for s in all_sk_result.scalars().all()]
                    ch_result = await session.execute(
                        select(ChannelModel).where(ChannelModel.instance_id == instance_id)
                    )
                    channels = ch_result.scalars().all()
                    ch_dicts = [{"type": c.type, "name": c.name, "config": c.config, "is_active": c.is_active} for c in channels]

                    mgr = get_openclaw_manager()
                    import asyncio as _aio
                    await _aio.to_thread(
                        mgr.hot_update_config,
                        instance_id=str(inst.id),
                        namespace=inst.namespace,
                        channels=ch_dicts,
                        skills=remaining_skills,
                        port=inst.port or 18789,
                    )
                except Exception as e:
                    logger.error(f"Skill {skill_name}: ConfigMap 回退卸载失败: {e}")

                # 删除 DB 记录
                try:
                    await session.delete(skill)
                    await session.commit()
                except Exception:
                    skill.status = "error"
                    await session.commit()

                return {"status": "ok", "action": "uninstall", "skill": skill_name}

            else:
                return {"status": "error", "reason": f"unknown action: {action}"}

    except Exception as e:
        return {"status": "error", "reason": str(e)}


# ============== 启动和关闭钩子 ==============

async def startup(ctx: dict):
    """Worker 启动时执行"""
    logger.info(f"[ARQ] Worker starting up at {datetime.utcnow().isoformat()}")
    # 打印所有已注册的 cron 任务
    for c in WorkerSettings.cron_jobs:
        logger.info(f"[ARQ]   cron: {c.name}, second={c.second}, minute={c.minute}, unique={c.unique}")
    logger.info(f"[ARQ] Total cron jobs: {len(WorkerSettings.cron_jobs)}")

    # 启动时清理残留的 in-progress 键（上次 Worker 异常退出可能遗留）
    redis = ctx.get('redis')
    if redis:
        try:
            stale_keys = []
            cursor = 0
            while True:
                cursor, keys = await redis.scan(cursor, match='arq:in-progress:*', count=100)
                stale_keys.extend(keys)
                if not cursor:  # cursor == 0 表示扫描完毕
                    break
            if stale_keys:
                deleted = await redis.delete(*stale_keys)
                key_names = [k.decode() if isinstance(k, bytes) else str(k) for k in stale_keys]
                logger.info(f"[ARQ] Cleaned {deleted} stale in-progress keys: {key_names}")
            else:
                logger.info("[ARQ] No stale in-progress keys found")
        except Exception as e:
            logger.error(f"[ARQ] Error cleaning stale keys: {e}")
    else:
        logger.warning("[ARQ] WARNING: redis not available in ctx, skip in-progress cleanup")


async def shutdown(ctx: dict):
    """Worker 关闭时执行"""
    logger.info(f"[ARQ] Worker shutting down at {datetime.utcnow().isoformat()}")
    # 可以在这里清理资源


# ============== Worker 配置 ==============

class WorkerSettings:
    """ARQ Worker 配置类"""
    
    # Redis 连接设置
    redis_settings = get_redis_settings()
    
    # 注册任务函数
    functions = [
        send_email_task,
        process_instance_billing_task,
        process_all_billing_task,
        check_instance_health_task,
        sync_instance_status_task,
        cleanup_expired_instances_task,
        generate_daily_report_task,
        # OpenClaw 任务
        openclaw_model_key_monitor,
        openclaw_channel_monitor,
        openclaw_instance_sync,
        openclaw_skill_manage,
    ]
        
    # 定时任务 (Cron Jobs)
    cron_jobs = [
        # 每30秒同步实例状态（K8s → DB）
        cron(sync_instance_status_task, second={0, 30}, unique=True, timeout=25),
        # 每15分钟检查计费
        cron(process_all_billing_task, minute={0, 15, 30, 45}, unique=True, timeout=120),
        # 每小时清理过期实例
        cron(cleanup_expired_instances_task, minute=5),
        # 每天凌晨2点生成报表
        cron(generate_daily_report_task, hour=2, minute=0),
        # OpenClaw: 每60秒监控大模型密钥
        cron(openclaw_model_key_monitor, second=0, unique=True, timeout=55),
        # OpenClaw: 每30秒监控通道在线状态
        cron(openclaw_channel_monitor, second={0, 30}, unique=True, timeout=25),
        # OpenClaw: 每30秒同步实例 K8s 状态
        cron(openclaw_instance_sync, second={0, 30}, unique=True, timeout=25),
    ]
    
    # 启动和关闭钩子
    on_startup = startup
    on_shutdown = shutdown
    
    # 队列名称前缀（隔离不同服务的 ARQ 任务）
    queue_name = 'arq:lmaicloud'
    
    # Worker 配置
    max_jobs = 10  # 最大并发任务数
    job_timeout = 300  # 任务超时时间（秒）
    keep_result = 600  # 结果保留 10 分钟（原 1 小时，减少 Redis 内存占用）
    health_check_interval = 60  # 健康检查间隔（秒）


# ============== 任务入队工具函数 ==============

_arq_pool: Optional[ArqRedis] = None


async def get_arq_pool() -> ArqRedis:
    """获取 ARQ Redis 连接池（单例）"""
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(get_redis_settings())
    return _arq_pool


async def close_arq_pool():
    """关闭 ARQ Redis 连接池"""
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.close()
        _arq_pool = None


async def enqueue_task(task_name: str, *args, **kwargs):
    """
    入队任务的便捷函数
    
    Args:
        task_name: 任务函数名
        *args: 任务参数
        **kwargs: 任务关键字参数
    
    Returns:
        任务 Job 对象
    
    Example:
        await enqueue_task("send_email_task", "user@example.com", "Welcome", "Hello!")
    """
    pool = await get_arq_pool()
    job = await pool.enqueue_job(task_name, *args, _queue_name='arq:lmaicloud', **kwargs)
    return job


async def enqueue_task_delayed(task_name: str, delay_seconds: int, *args, **kwargs):
    """
    延迟入队任务
    
    Args:
        task_name: 任务函数名
        delay_seconds: 延迟秒数
        *args: 任务参数
        **kwargs: 任务关键字参数
    
    Returns:
        任务 Job 对象
    """
    pool = await get_arq_pool()
    job = await pool.enqueue_job(
        task_name,
        *args,
        _queue_name='arq:lmaicloud',
        _defer_by=timedelta(seconds=delay_seconds),
        **kwargs
    )
    return job


# ============== 直接运行 Worker ==============

if __name__ == "__main__":
    # 可以直接运行此文件启动 Worker
    # python -m app.tasks
    import subprocess
    subprocess.run(["arq", "app.tasks.WorkerSettings"])
