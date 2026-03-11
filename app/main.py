from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.config import settings
from app.database import init_db
from app.tasks import get_arq_pool, close_arq_pool
from app.logging_config import setup_logging, get_logger
from app.api.v1 import auth, users, instances, storage, images, billing, market
from app.api.v1 import websocket as ws
from app.api.v1 import tickets, system
from app.api.v1.admin import clusters, nodes, admin_users, admin_orders, reports, admin_settings, admin_images, admin_tickets

# 初始化日志系统
logger = setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        await init_db()
        logger.info("数据库初始化成功")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        logger.warning("运行在无数据库模式")
    
    try:
        await get_arq_pool()
        logger.info("ARQ任务队列已连接")
    except Exception as e:
        logger.warning(f"Redis不可用: {e}")
    
    logger.info(f"应用启动完成 - {settings.app_name} v1.0.0")
    
    yield
    
    # Shutdown
    logger.info("应用正在关闭...")
    try:
        await close_arq_pool()
    except:
        pass
    logger.info("应用已关闭")


app = FastAPI(
    title=settings.app_name,
    description="""
## LMAICloud GPU算力云平台 API

LMAICloud 提供企业级GPU算力租用服务，支持以下功能：

### 核心功能
- **实例管理**: 创建、启动、停止、释放 GPU 实例
- **存储管理**: 文件上传、下载、删除等操作
- **计费系统**: 余额管理、充值、账单查询
- **市场服务**: 查看可用机器、GPU型号、区域信息

### 认证方式
使用 JWT Bearer Token 认证。登录成功后获取 access_token，
在请求头中添加 `Authorization: Bearer <token>`。

### 状态码说明
- `200`: 请求成功
- `400`: 请求参数错误
- `401`: 未认证或Token过期
- `403`: 无权限访问
- `404`: 资源不存在
- `500`: 服务器内部错误
    """,
    version="1.0.0",
    lifespan=lifespan,
    contact={
        "name": "LMAICloud Support",
        "email": "support@lmaicloud.com",
    },
    license_info={
        "name": "MIT",
    },
    openapi_tags=[
        {"name": "认证", "description": "用户注册、登录、Token管理"},
        {"name": "用户", "description": "用户信息管理"},
        {"name": "实例", "description": "GPU实例的创建、管理和操作"},
        {"name": "存储", "description": "文件存储管理"},
        {"name": "镜像", "description": "系统镜像列表"},
        {"name": "计费", "description": "余额、充值、账单管理"},
        {"name": "市场", "description": "可用机器和资源查询"},
        {"name": "WebSocket", "description": "实时状态推送"},
        {"name": "管理-集群", "description": "管理后台-集群管理"},
        {"name": "管理-节点", "description": "管理后台-节点管理"},
        {"name": "管理-用户", "description": "管理后台-用户管理"},
        {"name": "管理-订单", "description": "管理后台-订单管理"},
        {"name": "管理-报表", "description": "管理后台-数据报表"},
        {"name": "管理-设置", "description": "管理后台-系统设置"},
        {"name": "管理-应用镜像", "description": "管理后台-应用镜像管理"},
        {"name": "管理-工单", "description": "管理后台-工单管理"},
        {"name": "工单", "description": "用户工单提交与查看"},
    ],
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API v1 routes
app.include_router(auth.router, prefix="/api/v1/auth", tags=["认证"])
app.include_router(users.router, prefix="/api/v1/users", tags=["用户"])
app.include_router(instances.router, prefix="/api/v1/instances", tags=["实例"])
app.include_router(storage.router, prefix="/api/v1/storage", tags=["存储"])
app.include_router(images.router, prefix="/api/v1/images", tags=["镜像"])
app.include_router(billing.router, prefix="/api/v1/billing", tags=["计费"])
app.include_router(market.router, prefix="/api/v1/market", tags=["市场"])
app.include_router(tickets.router, prefix="/api/v1/tickets", tags=["工单"])
app.include_router(system.router, prefix="/api/v1/system", tags=["系统"])

# Admin routes
app.include_router(clusters.router, prefix="/api/v1/admin/clusters", tags=["管理-集群"])
app.include_router(nodes.router, prefix="/api/v1/admin/nodes", tags=["管理-节点"])
app.include_router(admin_users.router, prefix="/api/v1/admin/users", tags=["管理-用户"])
app.include_router(admin_orders.router, prefix="/api/v1/admin/orders", tags=["管理-订单"])
app.include_router(reports.router, prefix="/api/v1/admin/reports", tags=["管理-报表"])
app.include_router(admin_settings.router, prefix="/api/v1/admin/settings", tags=["管理-设置"])
app.include_router(admin_images.router, prefix="/api/v1/admin/images", tags=["管理-应用镜像"])
app.include_router(admin_tickets.router, prefix="/api/v1/admin/tickets", tags=["管理-工单"])

# WebSocket routes
app.include_router(ws.router, tags=["WebSocket"])


@app.get("/")
async def root():
    return {"message": "Welcome to LMAICloud API", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
