# LMAICloud Backend

LMAICloud GPU算力云平台后端服务，基于 FastAPI + PostgreSQL + Kubernetes 构建。

## 技术栈

- **FastAPI** - 高性能异步Web框架
- **SQLAlchemy 2.0** - 异步ORM
- **PostgreSQL** - 主数据库（asyncpg驱动）
- **Redis + ARQ** - 异步任务队列
- **Kubernetes** - GPU实例编排
- **WebSocket** - 实例状态实时推送
- **JWT** - 用户认证

## 项目结构

```
app/
├── api/v1/
│   ├── auth.py          # 认证（注册/登录/Token）
│   ├── instances.py     # GPU实例管理
│   ├── billing.py       # 计费/充值/支付
│   ├── storage.py       # 文件存储
│   ├── images.py        # 系统镜像
│   ├── market.py        # 算力市场
│   ├── users.py         # 用户信息
│   ├── websocket.py     # 实时状态推送
│   └── admin/           # 管理后台API
│       ├── clusters.py      # 集群管理
│       ├── nodes.py         # 节点管理
│       ├── admin_users.py   # 用户管理
│       ├── admin_orders.py  # 订单管理
│       ├── reports.py       # 数据报表
│       └── admin_settings.py # 系统设置
├── models/              # 数据库模型
├── schemas/             # Pydantic Schemas
├── services/
│   ├── k8s_client.py    # Kubernetes客户端
│   ├── pod_manager.py   # Pod生命周期管理
│   ├── ws_manager.py    # WebSocket连接管理
│   ├── monitoring.py    # 节点监控
│   └── scheduler.py     # 实例调度器
└── utils/               # 工具函数
tests/                   # 单元测试
```

## 快速启动

### 方式一：uv + venv（推荐，当前环境使用的是这个）

```bash
# 安装 uv（若未安装）
pip install uv

# 创建虚拟环境
uv venv .venv

# 激活虚拟环境
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# 安装依赖（比 pip 快 10x）
uv pip install -r requirements.txt
```

### 方式二：pip + venv

```bash
python -m venv .venv

# 激活虚拟环境
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填写数据库、Redis等配置
```

### 3. 启动数据库

```bash
# PostgreSQL
docker run -d --name postgres \
  -e POSTGRES_DB=lmaicloud \
  -e POSTGRES_PASSWORD=password \
  -p 5432:5432 postgres:16

# Redis
docker run -d --name redis -p 6379:6379 redis:7
```

### 4. 初始化数据库

```bash
python -m app.database
```

### 5. 启动服务

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API文档：http://localhost:8000/docs
http://115.190.25.82:8883/gpucloudapi/docs

## 运行测试

```bash
pytest
# 带覆盖率
pytest --cov=app tests/
```

## 主要API

| 模块 | 路径前缀 | 说明 |
|------|---------|------|
| 认证 | `/api/v1/auth` | 注册、登录、Token刷新 |
| 实例 | `/api/v1/instances` | GPU实例 CRUD + 操作 |
| 计费 | `/api/v1/billing` | 余额、充值、订单 |
| 存储 | `/api/v1/storage` | 文件上传下载 |
| 市场 | `/api/v1/market` | 可用机器列表 |
| WebSocket | `/ws/status` | 实例状态实时订阅 |
| 管理后台 | `/api/v1/admin/*` | 集群/节点/用户/报表 |

## Kubernetes 配置

后端通过 `~/.kube/config` 自动连接集群，或通过环境变量 `KUBECONFIG` 指定路径。
无K8s环境时降级为模拟模式运行（适用于开发调试）。

## 环境变量说明

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATABASE_URL` | PostgreSQL连接串 | `postgresql+asyncpg://postgres:password@localhost:5432/lmaicloud` |
| `JWT_SECRET_KEY` | JWT签名密钥 | 需修改 |
| `REDIS_URL` | Redis连接串 | `redis://localhost:6379` |
| `CORS_ORIGINS` | 允许跨域来源 | `http://localhost:3000` |
| `APP_ENV` | 运行环境 | `development` |
