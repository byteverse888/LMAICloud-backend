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


async def process_instance_billing_task(ctx: dict, instance_id: str) -> dict:
    """
    处理单个实例计费任务 - 扣除一小时费用
    """
    from app.database import AsyncSessionLocal
    from app.models import Instance, User, Order, OrderType, OrderStatus
    from sqlalchemy import select
    from datetime import datetime
    
    print(f"[BILLING] Processing billing for instance: {instance_id}")
    
    async with AsyncSessionLocal() as session:
        # 获取实例
        result = await session.execute(select(Instance).where(Instance.id == instance_id))
        instance = result.scalar_one_or_none()
        
        if not instance or instance.status != "running":
            return {"status": "skipped", "reason": "instance not running"}
        
        # 获取用户
        result = await session.execute(select(User).where(User.id == instance.user_id))
        user = result.scalar_one_or_none()
        
        if not user:
            return {"status": "error", "reason": "user not found"}
        
        # 计算费用
        hourly_price = instance.hourly_price
        
        # 检查余额
        if user.balance < hourly_price:
            # 余额不足，发送警告，但不立即停机
            print(f"[BILLING] User {user.id} balance insufficient: {user.balance} < {hourly_price}")
            # TODO: 发送余额不足通知
            # 如果余额严重不足（负值超过10元），标记实例即将释放
            if user.balance < -10:
                instance.status = "expired"
                await session.commit()
                return {"status": "expired", "reason": "balance too low"}
        
        # 扣费
        user.balance -= hourly_price
        
        # 创建订单记录
        order = Order(
            user_id=user.id,
            instance_id=instance.id,
            type=OrderType.RENEW,
            amount=-hourly_price,
            status=OrderStatus.PAID,
            paid_at=datetime.utcnow(),
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
    圴时任务: 处理所有运行中实例的计费
    """
    from app.database import AsyncSessionLocal
    from app.models import Instance
    from sqlalchemy import select
    
    print(f"[BILLING] Processing all instance billing...")
    
    processed = 0
    errors = 0
    
    async with AsyncSessionLocal() as session:
        # 查询所有运行中的实例
        result = await session.execute(
            select(Instance).where(Instance.status == "running")
        )
        instances = result.scalars().all()
        
        for instance in instances:
            try:
                result = await process_instance_billing_task(ctx, str(instance.id))
                if result.get("status") == "processed":
                    processed += 1
            except Exception as e:
                print(f"[BILLING] Error processing {instance.id}: {e}")
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
    # 1. SSH 连接测试
    # 2. GPU 状态检查
    # 3. 更新实例健康状态
    
    return {
        "status": "healthy",
        "instance_id": instance_id,
        "checked_at": datetime.utcnow().isoformat(),
    }


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
        cleanup_expired_instances_task,
        generate_daily_report_task,
    ]
        
    # 定时任务 (Cron Jobs)
    cron_jobs = [
        # 每小时整点计费
        cron(process_all_billing_task, minute=0),
        # 每小时清理过期实例
        cron(cleanup_expired_instances_task, minute=5),
        # 每天凌晨2点生成报表
        cron(generate_daily_report_task, hour=2, minute=0),
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
