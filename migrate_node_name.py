"""数据库迁移 - instances 表支持 K8s 直查节点"""
import asyncio
from sqlalchemy import text
from app.database import engine


async def migrate():
    async with engine.begin() as conn:
        # 1. node_id 改为可空
        try:
            await conn.execute(text(
                "ALTER TABLE instances ALTER COLUMN node_id DROP NOT NULL"
            ))
            print("✓ node_id 已改为可空")
        except Exception as e:
            print(f"node_id 可能已是可空: {e}")

        # 2. 新增 node_name 列
        try:
            await conn.execute(text(
                "ALTER TABLE instances ADD COLUMN IF NOT EXISTS node_name VARCHAR(100)"
            ))
            print("✓ node_name 列已添加")
        except Exception as e:
            print(f"node_name 列可能已存在: {e}")

        # 3. 回填 node_name
        try:
            result = await conn.execute(text("""
                UPDATE instances SET node_name = nodes.name
                FROM nodes WHERE instances.node_id = nodes.id AND instances.node_name IS NULL
            """))
            print(f"✓ 回填 node_name 完成，更新 {result.rowcount} 行")
        except Exception as e:
            print(f"回填失败（可忽略）: {e}")

        print("\n迁移完成!")


if __name__ == "__main__":
    asyncio.run(migrate())
