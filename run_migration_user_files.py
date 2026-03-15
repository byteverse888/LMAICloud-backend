"""数据库迁移脚本 - 用户文件管理系统"""
import asyncio
from sqlalchemy import text
from app.database import engine


async def migrate():
    async with engine.begin() as conn:
        # 1. AIUser 新增存储配额字段
        for col, typedef in [
            ("storage_quota", "BIGINT DEFAULT 10737418240"),
            ("storage_used", "BIGINT DEFAULT 0"),
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS {col} {typedef}"))
                print(f"✓ ai_users.{col} 添加成功")
            except Exception as e:
                print(f"  ai_users.{col} 可能已存在: {e}")

        # 2. 新建 user_files 表
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS user_files (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES ai_users(id),
                    parent_id UUID REFERENCES user_files(id) ON DELETE CASCADE,
                    name VARCHAR(255) NOT NULL,
                    path VARCHAR(1000) NOT NULL,
                    is_dir BOOLEAN DEFAULT FALSE,
                    size BIGINT DEFAULT 0,
                    mime_type VARCHAR(100),
                    storage_backend VARCHAR(20) DEFAULT 'ipfs',
                    storage_key VARCHAR(500),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    CONSTRAINT uq_user_parent_name UNIQUE(user_id, parent_id, name)
                )
            """))
            print("✓ user_files 表创建成功")
        except Exception as e:
            print(f"  user_files 表可能已存在: {e}")

        # 3. 根目录唯一索引 (PG 中 NULL!=NULL, UNIQUE 约束对 NULL 列不生效)
        try:
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_user_root_name
                    ON user_files(user_id, name) WHERE parent_id IS NULL
            """))
            print("✓ uq_user_root_name 索引创建成功")
        except Exception as e:
            print(f"  uq_user_root_name 索引可能已存在: {e}")

        # 4. 辅助索引
        for idx_name, idx_def in [
            ("idx_user_files_user_parent", "user_files(user_id, parent_id)"),
            ("idx_user_files_path", "user_files(user_id, path)"),
        ]:
            try:
                await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}"))
                print(f"✓ 索引 {idx_name} 创建成功")
            except Exception as e:
                print(f"  索引 {idx_name} 可能已存在: {e}")

        print("\n✅ 用户文件管理系统迁移完成!")


if __name__ == "__main__":
    asyncio.run(migrate())
