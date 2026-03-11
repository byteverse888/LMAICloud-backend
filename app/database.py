from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy import text, select
import asyncpg
from datetime import datetime
from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    future=True,
)

async_session_maker = async_sessionmaker(
    engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)

# 别名，用于后台任务
AsyncSessionLocal = async_session_maker

Base = declarative_base()


async def get_db():
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def ensure_database_exists():
    """自动创建数据库（若不存在）"""
    import logging
    logger = logging.getLogger("lmaicloud.database")
    
    db_url = settings.database_url
    # 解析出数据库名和连接到 postgres 默认库的 URL
    # postgresql+asyncpg://user:pass@host:port/dbname -> 替换为 /postgres
    db_name = db_url.split("/")[-1]
    system_url = db_url.rsplit("/", 1)[0] + "/postgres"
    # asyncpg 原生连接（不走 SQLAlchemy）
    system_url_raw = system_url.replace("postgresql+asyncpg://", "")
    try:
        conn = await asyncpg.connect(f"postgresql://{system_url_raw}")
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", db_name
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            logger.info(f"数据库 '{db_name}' 自动创建成功")
        await conn.close()
    except Exception as e:
        logger.warning(f"无法自动创建数据库: {e}")


async def init_db():
    await ensure_database_exists()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    # 创建默认用户
    await create_default_users()


async def create_default_users():
    """创建默认用户（若不存在）"""
    import uuid
    import logging
    from app.models import AIUser, UserRole, UserStatus
    from passlib.context import CryptContext
    
    logger = logging.getLogger("lmaicloud.database")
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    
    default_users = [
        {
            "email": "test@example.com",
            "nickname": "测试用户",
            "password": "Test@1234",
            "balance": 1000.0,
            "role": UserRole.USER,
        },
        {
            "email": "admin@example.com",
            "nickname": "管理员",
            "password": "Admin@1234",
            "balance": 10000.0,
            "role": UserRole.ADMIN,
        },
    ]
    
    async with async_session_maker() as session:
        for user_data in default_users:
            result = await session.execute(
                select(AIUser).where(AIUser.email == user_data["email"])
            )
            if not result.scalar_one_or_none():
                user = AIUser(
                    id=uuid.uuid4(),
                    email=user_data["email"],
                    nickname=user_data["nickname"],
                    password_hash=pwd_context.hash(user_data["password"]),
                    balance=user_data["balance"],
                    role=user_data["role"],
                    status=UserStatus.ACTIVE,
                    verified=True,  # 默认用户无需邮箱激活
                    created_at=datetime.utcnow(),
                )
                session.add(user)
                logger.info(f"创建默认用户: {user_data['email']}")
        await session.commit()
