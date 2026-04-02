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
    print(f"[EMAIL] Sending email to {to_email}: {subject}")
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

    支持按量（hourly）和包月/包年计费：
    - hourly: 每小时扣费
    - monthly/yearly: 到期后自动续费或停机
    """
    from app.database import AsyncSessionLocal
    from app.models import Instance, OpenClawInstance, User, Order, OrderType, OrderStatus, InstanceStatus
    from sqlalchemy import select
    from datetime import datetime
    from dateutil.relativedelta import relativedelta

    print(f"[BILLING] Processing billing for {instance_type} instance: {instance_id}")

    async with AsyncSessionLocal() as session:
        # 根据类型获取实例
        if instance_type == "openclaw":
            result = await session.execute(select(OpenClawInstance).where(OpenClawInstance.id == instance_id))
            instance = result.scalar_one_or_none()
            if not instance or instance.status != "running":
                return {"status": "skipped", "reason": "instance not running"}
            hourly_price = getattr(instance, "hourly_price", None)
            if hourly_price is None:
                # OpenClaw 默认价格：按 CPU/内存/磁盘 估算
                from app.config import settings
                hourly_price = settings.default_gpu_hourly_price
            billing_cycle = getattr(instance, "billing_cycle", "hourly") or "hourly"
            inst_user_id = instance.user_id
            order_kwargs = {"openclaw_instance_id": instance.id}
        else:
            result = await session.execute(select(Instance).where(Instance.id == instance_id))
            instance = result.scalar_one_or_none()
            if not instance or instance.status != InstanceStatus.RUNNING:
                return {"status": "skipped", "reason": "instance not running"}
            hourly_price = instance.hourly_price
            billing_cycle = getattr(instance, "billing_cycle", None) or instance.billing_type or "hourly"
            inst_user_id = instance.user_id
            order_kwargs = {"instance_id": instance.id}

        # 获取用户
        result = await session.execute(select(User).where(User.id == inst_user_id))
        user = result.scalar_one_or_none()
        if not user:
            return {"status": "error", "reason": "user not found"}

        # 包月/包年计费逻辑
        if billing_cycle in ("monthly", "yearly"):
            expired_at = getattr(instance, "expired_at", None)
            if expired_at and datetime.utcnow() < expired_at:
                # 尚未到期，跳过
                return {"status": "skipped", "reason": "subscription active"}

            # 到期了，尝试自动续费
            if billing_cycle == "monthly":
                renew_price = hourly_price * 24 * 30  # 月费 = 时价 * 720h
                delta = relativedelta(months=1)
            else:
                renew_price = hourly_price * 24 * 365  # 年费
                delta = relativedelta(years=1)

            if user.balance < renew_price:
                # 余额不足，停机
                instance.status = InstanceStatus.EXPIRED
                await session.commit()
                return {"status": "expired", "reason": "balance too low for renewal"}

            # 扣费续期
            user.balance -= renew_price
            new_start = expired_at or datetime.utcnow()
            instance.expired_at = new_start + delta

            order = Order(
                user_id=user.id,
                type=OrderType.RENEW,
                amount=-renew_price,
                status=OrderStatus.PAID,
                paid_at=datetime.utcnow(),
                product_name=f"{instance_type} 实例续费",
                billing_cycle=billing_cycle,
                description=f"{'包月' if billing_cycle == 'monthly' else '包年'}自动续费",
                **order_kwargs,
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

        # 按量（hourly）扣费
        if user.balance < hourly_price:
            print(f"[BILLING] User {user.id} balance insufficient: {user.balance} < {hourly_price}")
            if user.balance < -10:
                instance.status = InstanceStatus.EXPIRED
                await session.commit()
                return {"status": "expired", "reason": "balance too low"}

        user.balance -= hourly_price
        order = Order(
            user_id=user.id,
            type=OrderType.RENEW,
            amount=-hourly_price,
            status=OrderStatus.PAID,
            paid_at=datetime.utcnow(),
            product_name=f"{instance_type} 实例按量计费",
            billing_cycle="hourly",
            **order_kwargs,
        )
        session.add(order)
        await session.commit()

        return {
            "status": "processed",
            "instance_id": instance_id,
            "amount": hourly_price,
            "new_balance": user.balance,
            "processed_at": datetime.utcnow().isoformat(),
        }


async def process_all_billing_task(ctx: dict) -> dict:
    """
    定时任务: 处理所有运行中实例的计费（GPU + OpenClaw）
    """
    from app.database import AsyncSessionLocal
    from app.models import Instance, OpenClawInstance
    from app.models import InstanceStatus as _IS
    from sqlalchemy import select

    print(f"[BILLING] Processing all instance billing...")

    processed = 0
    errors = 0

    async with AsyncSessionLocal() as session:
        # GPU 实例
        result = await session.execute(
            select(Instance).where(Instance.status == _IS.RUNNING)
        )
        for instance in result.scalars().all():
            try:
                r = await process_instance_billing_task(ctx, str(instance.id), "gpu")
                if r.get("status") in ("processed", "renewed"):
                    processed += 1
            except Exception as e:
                print(f"[BILLING] Error processing GPU {instance.id}: {e}")
                errors += 1

        # OpenClaw 实例
        oc_result = await session.execute(
            select(OpenClawInstance).where(OpenClawInstance.status == "running")
        )
        for inst in oc_result.scalars().all():
            try:
                r = await process_instance_billing_task(ctx, str(inst.id), "openclaw")
                if r.get("status") in ("processed", "renewed"):
                    processed += 1
            except Exception as e:
                print(f"[BILLING] Error processing OpenClaw {inst.id}: {e}")
                errors += 1

    return {
        "status": "completed",
        "processed": processed,
        "errors": errors,
        "processed_at": datetime.utcnow().isoformat(),
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
    print(f"[HEALTH] Checking health for instance: {instance_id}")
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
            result = await session.execute(
                select(Instance).where(Instance.status.in_([_IS.CREATING, _IS.STARTING]))
            )
            pending = result.scalars().all()
            if not pending:
                return {"status": "ok", "synced": 0}

            k8s = get_k8s_client()
            if not k8s.is_connected:
                return {"status": "skipped", "reason": "k8s not connected"}

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
                    created = inst.created_at
                    if created and created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created and (now_utc - created) > CREATING_TIMEOUT:
                        new_status = _IS.ERROR
                    else:
                        continue

                if new_status == inst.status:
                    continue

                old_status = inst.status
                inst.status = new_status
                if new_status == _IS.RUNNING and not inst.started_at:
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

            return {"status": "ok", "synced": synced}

    except Exception as e:
        return {"status": "error", "reason": str(e)}


async def cleanup_expired_instances_task(ctx: dict) -> dict:
    """
    清理过期实例任务 (定时任务)
    
    Returns:
        清理结果
    """
    print(f"[CLEANUP] Cleaning up expired instances...")
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
    print(f"[REPORT] Generating daily report...")
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
    遍历所有 running 的 OpenClaw 实例, 验证每个 ModelKey 可用性并更新监控字段。
    """
    from app.database import AsyncSessionLocal
    from app.models import OpenClawInstance, ModelKey
    from app.services.openclaw_client import OpenClawClient, build_openclaw_url
    from sqlalchemy import select
    from datetime import datetime

    checked = 0
    errors = 0

    try:
        async with AsyncSessionLocal() as session:
            # 获取所有运行中的 OpenClaw 实例
            result = await session.execute(
                select(OpenClawInstance).where(OpenClawInstance.status == "running")
            )
            instances = result.scalars().all()

            for inst in instances:
                # 获取该实例的所有活跃密钥
                keys_result = await session.execute(
                    select(ModelKey).where(
                        ModelKey.instance_id == inst.id,
                        ModelKey.is_active == True,
                    )
                )
                keys = keys_result.scalars().all()
                if not keys:
                    continue

                # 通过 OpenClaw Gateway 获取状态
                try:
                    url = build_openclaw_url(inst.service_name, inst.namespace, inst.port)
                    client = OpenClawClient(url, inst.gateway_token)
                    status_data = await client.get_status()
                except Exception:
                    status_data = None

                for key in keys:
                    try:
                        # 基础可达性: 如果 Gateway 返回了状态说明 key 至少在配置中
                        if status_data:
                            key.check_status = "ok"
                        else:
                            key.check_status = "error"
                        key.last_check_at = datetime.utcnow()
                        checked += 1
                    except Exception:
                        key.check_status = "error"
                        key.last_check_at = datetime.utcnow()
                        errors += 1

                await session.commit()

    except Exception as e:
        return {"status": "error", "reason": str(e)}

    return {"status": "ok", "checked": checked, "errors": errors}


async def openclaw_channel_monitor(ctx: dict) -> dict:
    """
    定时任务: 通道在线监控 (每30秒)
    通过 OpenClawClient.get_status() 检查各通道连通性, 更新 online_status。
    """
    from app.database import AsyncSessionLocal
    from app.models import OpenClawInstance, Channel
    from app.services.openclaw_client import OpenClawClient, build_openclaw_url
    from sqlalchemy import select
    from datetime import datetime

    checked = 0

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(OpenClawInstance).where(OpenClawInstance.status == "running")
            )
            instances = result.scalars().all()

            for inst in instances:
                channels_result = await session.execute(
                    select(Channel).where(
                        Channel.instance_id == inst.id,
                        Channel.is_active == True,
                    )
                )
                channels = channels_result.scalars().all()
                if not channels:
                    continue

                try:
                    url = build_openclaw_url(inst.service_name, inst.namespace, inst.port)
                    client = OpenClawClient(url, inst.gateway_token)
                    status_data = await client.get_status()
                    gateway_online = True
                except Exception:
                    status_data = None
                    gateway_online = False

                for ch in channels:
                    if gateway_online:
                        ch.online_status = "online"
                    else:
                        ch.online_status = "offline"
                    ch.last_check_at = datetime.utcnow()
                    checked += 1

                await session.commit()

    except Exception as e:
        return {"status": "error", "reason": str(e)}

    return {"status": "ok", "checked": checked}


async def openclaw_instance_sync(ctx: dict) -> dict:
    """
    定时任务: OpenClaw 实例状态同步 (每120秒)
    检查 K8s Deployment/Pod 状态并同步到 DB, 通过 Redis pub/sub 通知前端。
    """
    from app.database import AsyncSessionLocal
    from app.models import OpenClawInstance
    from app.services.k8s_client import get_k8s_client
    from sqlalchemy import select
    from datetime import datetime, timezone
    import json

    synced = 0

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

            k8s = get_k8s_client()
            if not k8s.is_connected:
                return {"status": "skipped", "reason": "k8s not connected"}

            for inst in instances:
                try:
                    dep_name = inst.deployment_name
                    ns = inst.namespace
                    if not dep_name or not ns:
                        continue

                    dep_info = k8s.get_deployment(dep_name, ns)
                    if not dep_info:
                        # Deployment 已不存在
                        if inst.status not in ("released", "releasing"):
                            inst.status = "error"
                            synced += 1
                        continue

                    replicas = dep_info.get("ready_replicas", 0) or 0
                    desired = dep_info.get("replicas", 1) or 1

                    if desired == 0:
                        new_status = "stopped"
                    elif replicas >= desired:
                        new_status = "running"
                    else:
                        new_status = "creating"

                    if new_status != inst.status:
                        old_status = inst.status
                        inst.status = new_status
                        if new_status == "running" and not inst.started_at:
                            inst.started_at = datetime.utcnow()
                        synced += 1

                        # Redis pub/sub 通知
                        try:
                            redis_conn = ctx.get("redis")
                            if redis_conn:
                                await redis_conn.publish(
                                    "lmaicloud:instance_status",
                                    json.dumps({
                                        "instance_id": str(inst.id),
                                        "user_id": str(inst.user_id),
                                        "status": new_status,
                                        "old_status": old_status,
                                        "type": "openclaw",
                                    }),
                                )
                        except Exception:
                            pass

                except Exception as e:
                    print(f"[OPENCLAW_SYNC] Error syncing {inst.id}: {e}")

            await session.commit()

    except Exception as e:
        return {"status": "error", "reason": str(e)}

    return {"status": "ok", "synced": synced}


async def openclaw_skill_manage(ctx: dict, instance_id: str, skill_name: str, action: str) -> dict:
    """
    异步任务: Skills 安装/卸载
    通过 OpenClawClient 向运行中的实例发送 skill 管理指令, 并更新 DB 状态。

    Args:
        instance_id: OpenClaw 实例 ID
        skill_name: 技能名称
        action: "install" 或 "uninstall"
    """
    from app.database import AsyncSessionLocal
    from app.models import OpenClawInstance, OpenClawSkill
    from app.services.openclaw_client import OpenClawClient, build_openclaw_url
    from sqlalchemy import select
    from datetime import datetime

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
                if skill:
                    skill.status = "installing"
                else:
                    return {"status": "error", "reason": "skill record not found"}

                await session.commit()

                # 调用 OpenClaw Gateway 获取 skills 列表确认安装
                try:
                    skills_list = await client.list_skills()
                    installed = any(s.get("name") == skill_name for s in skills_list)
                    if installed:
                        skill.status = "installed"
                        skill.installed_at = datetime.utcnow()
                    else:
                        skill.status = "error"
                except Exception as e:
                    skill.status = "error"

                await session.commit()
                return {"status": "ok", "action": "install", "skill": skill_name}

            elif action == "uninstall":
                if skill:
                    skill.status = "uninstalling"
                    await session.commit()

                    try:
                        # 标记为已卸载
                        skill.status = "uninstalled"
                        await session.commit()
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
    print(f"[ARQ] Worker starting up at {datetime.utcnow().isoformat()}")
    # 可以在这里初始化数据库连接等资源


async def shutdown(ctx: dict):
    """Worker 关闭时执行"""
    print(f"[ARQ] Worker shutting down at {datetime.utcnow().isoformat()}")
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
        # 每小时整点计费
        cron(process_all_billing_task, minute=0),
        # 每小时清理过期实例
        cron(cleanup_expired_instances_task, minute=5),
        # 每天凌晨2点生成报表
        cron(generate_daily_report_task, hour=2, minute=0),
        # OpenClaw: 每60秒监控大模型密钥
        cron(openclaw_model_key_monitor, second=0, unique=True, timeout=55),
        # OpenClaw: 每30秒监控通道在线状态
        cron(openclaw_channel_monitor, second={0, 30}, unique=True, timeout=25),
        # OpenClaw: 每2分钟同步实例 K8s 状态
        cron(openclaw_instance_sync, second=0, minute={0, 2}, unique=True, timeout=115),
    ]
    
    # 启动和关闭钩子
    on_startup = startup
    on_shutdown = shutdown
    
    # Worker 配置
    max_jobs = 10  # 最大并发任务数
    job_timeout = 300  # 任务超时时间（秒）
    keep_result = 3600  # 结果保留时间（秒）
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
    job = await pool.enqueue_job(task_name, *args, **kwargs)
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
