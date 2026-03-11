"""数据库迁移脚本 - 添加邮箱激活字段"""
import asyncio
from sqlalchemy import text
from app.database import engine

async def migrate():
    async with engine.begin() as conn:
        # 添加 activation_token 字段
        try:
            await conn.execute(text("""
                ALTER TABLE ai_users 
                ADD COLUMN IF NOT EXISTS activation_token VARCHAR(100)
            """))
            print("✓ 添加 activation_token 字段成功")
        except Exception as e:
            print(f"activation_token 字段可能已存在: {e}")
        
        # 添加 activation_expires_at 字段
        try:
            await conn.execute(text("""
                ALTER TABLE ai_users 
                ADD COLUMN IF NOT EXISTS activation_expires_at TIMESTAMP
            """))
            print("✓ 添加 activation_expires_at 字段成功")
        except Exception as e:
            print(f"activation_expires_at 字段可能已存在: {e}")
        
        # 创建索引
        try:
            await conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_ai_users_activation_token 
                ON ai_users(activation_token)
            """))
            print("✓ 创建索引成功")
        except Exception as e:
            print(f"索引可能已存在: {e}")
        
        print("\n数据库迁移完成!")

if __name__ == "__main__":
    asyncio.run(migrate())
