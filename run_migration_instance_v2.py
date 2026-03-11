"""
数据库迁移脚本 - Instance 表新增字段
适用于已部署过旧版本，需要升级 Instance 表结构的场景。
全新部署无需执行此脚本（启动时自动建表）。

用法: python run_migration_instance_v2.py
"""
import asyncio
from sqlalchemy import text
from app.database import engine

MIGRATIONS = [
    # 资源与节点
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS gpu_model VARCHAR(100)",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS resource_type VARCHAR(20) DEFAULT 'vGPU'",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS node_type VARCHAR(20) DEFAULT 'center'",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS instance_count INTEGER DEFAULT 1",
    # 镜像与启动
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS image_url VARCHAR(500)",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS startup_command VARCHAR(2000)",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS env_vars TEXT",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS storage_mounts TEXT",
    # 安装源
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS pip_source VARCHAR(100)",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS conda_source VARCHAR(100)",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS apt_source VARCHAR(100)",
    # 自动关机/释放
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_shutdown_type VARCHAR(20) DEFAULT 'none'",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_shutdown_minutes INTEGER",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_shutdown_time TIMESTAMP",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_release_type VARCHAR(20) DEFAULT 'none'",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_release_minutes INTEGER",
    # 连接与状态
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS internal_ip VARCHAR(50)",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS deployment_yaml TEXT",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS health_status VARCHAR(20) DEFAULT 'unknown'",
    "ALTER TABLE instances ADD COLUMN IF NOT EXISTS release_at TIMESTAMP",
]


async def run():
    async with engine.begin() as conn:
        for sql in MIGRATIONS:
            col = sql.split("ADD COLUMN IF NOT EXISTS ")[1].split(" ")[0]
            try:
                await conn.execute(text(sql))
                print(f"  [OK] {col}")
            except Exception as e:
                print(f"  [SKIP] {col} - {e}")
    print("\n迁移完成。")


if __name__ == "__main__":
    print("开始迁移 Instance 表...")
    asyncio.run(run())
