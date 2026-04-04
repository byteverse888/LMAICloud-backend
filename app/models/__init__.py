import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, DateTime, Enum, Boolean, ForeignKey, Integer, Text, BigInteger, UniqueConstraint, Index, text, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    BANNED = "banned"


class AIUser(Base):
    """AI云平台用户表（与parse系统users表隔离）"""
    __tablename__ = "ai_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    nickname = Column(String(100))
    avatar = Column(String(500))
    role = Column(Enum(UserRole), default=UserRole.USER)
    balance = Column(Float, default=0.0)
    frozen_balance = Column(Float, default=0.0)
    status = Column(Enum(UserStatus), default=UserStatus.ACTIVE)
    verified = Column(Boolean, default=False)  # 邮箱是否验证
    activation_token = Column(String(100), nullable=True, index=True)  # 邮箱激活令牌
    activation_expires_at = Column(DateTime, nullable=True)  # 激活令牌过期时间
    storage_quota = Column(BigInteger, default=10 * 1024**3)  # 总存储配额(字节), 默认10GB
    storage_used = Column(BigInteger, default=0)               # 已用存储空间(字节)
    # 积分系统
    points = Column(Integer, default=0)  # 积分余额
    invite_code = Column(String(20), unique=True, nullable=True, index=True)  # 邀请码
    invited_by = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=True)  # 邀请人
    last_checkin_date = Column(String(10), nullable=True)  # 最后签到日期 YYYY-MM-DD
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    instances = relationship("Instance", back_populates="ai_user")
    orders = relationship("Order", back_populates="ai_user")
    recharges = relationship("Recharge", back_populates="ai_user")
    files = relationship("UserFile", back_populates="owner")
    point_records = relationship("PointRecord", back_populates="user")
    notifications = relationship("Notification", back_populates="user")
    inviter = relationship("AIUser", remote_side="AIUser.id", foreign_keys=[invited_by])


# 保留User别名用于兼容（实际指向AIUser）
User = AIUser


class ClusterStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"


class Cluster(Base):
    __tablename__ = "clusters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    region = Column(String(50), nullable=False)
    status = Column(Enum(ClusterStatus), default=ClusterStatus.ONLINE)
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    nodes = relationship("Node", back_populates="cluster")


class NodeType(str, enum.Enum):
    CENTER = "center"
    EDGE = "edge"


class NodeStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"


class Node(Base):
    __tablename__ = "nodes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cluster_id = Column(UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=False)
    name = Column(String(100), nullable=False)
    region = Column(String(50))  # 区域
    type = Column(Enum(NodeType), default=NodeType.CENTER)
    status = Column(Enum(NodeStatus), default=NodeStatus.ONLINE)
    gpu_model = Column(String(100), nullable=False)
    gpu_count = Column(Integer, nullable=False)  # 总卡数
    gpu_total = Column(Integer, nullable=False, default=8)  # 总卡数别名
    gpu_available = Column(Integer, nullable=False)  # 可用卡数
    gpu_memory = Column(Integer, default=24)  # GB
    cpu_model = Column(String(100))
    cpu_cores = Column(Integer)
    memory = Column(Integer)  # GB
    disk = Column(Integer)  # GB
    disk_expandable = Column(Integer)  # GB
    ip_address = Column(String(50))
    gpu_driver = Column(String(50))  # driver_version 别名
    driver_version = Column(String(50))
    cuda_version = Column(String(20))
    hourly_price = Column(Float, nullable=False)
    available_until = Column(DateTime)  # 可租用截止日期
    tag = Column(String(50))  # 标签：cache/longterm等
    online_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cluster = relationship("Cluster", back_populates="nodes")
    instances = relationship("Instance", back_populates="node")


class InstanceStatus(str, enum.Enum):
    CREATING = "creating"
    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    ERROR = "error"
    EXPIRED = "expired"
    RELEASING = "releasing"
    RELEASED = "released"


class BillingType(str, enum.Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class AutoShutdownType(str, enum.Enum):
    NONE = "none"           # 不关机
    TIMER = "timer"         # 定时关机(N分钟后)
    SCHEDULED = "scheduled" # 指定时间关机


class AutoReleaseType(str, enum.Enum):
    NONE = "none"           # 不释放
    TIMER = "timer"         # 定时释放(N分钟后)


class ResourceType(str, enum.Enum):
    VGPU = "vGPU"
    NO_GPU = "no_gpu"       # 无卡启动


class Instance(Base):
    __tablename__ = "instances"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False)
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id"), nullable=True)
    node_name = Column(String(100), nullable=True)
    name = Column(String(100), nullable=False)
    status = Column(Enum(InstanceStatus), default=InstanceStatus.CREATING)

    # 资源配置
    gpu_count = Column(Integer, nullable=False)
    gpu_model = Column(String(100))  # GPU型号(冗余存储)
    cpu_cores = Column(Integer)
    memory = Column(Integer)  # GB
    disk = Column(Integer)  # GB
    resource_type = Column(String(20), default="vGPU")  # vGPU / no_gpu
    node_type = Column(String(10), default="center")  # center / edge
    instance_count = Column(Integer, default=1)  # 实例数量

    # 镜像与启动配置
    image_id = Column(UUID(as_uuid=True), ForeignKey("images.id"))
    image_url = Column(String(500))  # 镜像地址(冗余/自定义镜像)
    startup_command = Column(Text)  # 用户自定义启动命令
    env_vars = Column(Text)  # 环境变量 JSON: [{"key":"K","value":"V"}]
    storage_mounts = Column(Text)  # 存储挂载 JSON: [{"name":"data","mount_path":"/data","size_gb":50}]

    # 安装源
    pip_source = Column(String(50), default="default")   # pip源
    conda_source = Column(String(50), default="default") # conda源
    apt_source = Column(String(50), default="default")   # apt源

    # 计费
    billing_type = Column(Enum(BillingType), default=BillingType.HOURLY)
    hourly_price = Column(Float, nullable=False)

    # 自动关机/释放
    auto_shutdown_type = Column(String(20), default="none")  # none/timer/scheduled
    auto_shutdown_minutes = Column(Integer, nullable=True)    # 定时关机分钟数
    auto_shutdown_time = Column(DateTime, nullable=True)      # 指定关机时间
    auto_release_type = Column(String(20), default="none")    # none/timer
    auto_release_minutes = Column(Integer, nullable=True)     # 定时释放分钟数

    # K8s 命名空间（按用户隔离）
    namespace = Column(String(63), default="lmaicloud")  # K8s namespace, 格式: lmai-{user_id[:8]}

    # 连接信息
    ssh_host = Column(String(100))
    ssh_port = Column(Integer)
    ssh_password = Column(String(100))
    internal_ip = Column(String(50))  # 内网IP
    health_status = Column(String(20), default="unknown")

    # Deployment YAML 存档
    deployment_yaml = Column(Text)  # 生成的完整YAML存档

    # 时间
    started_at = Column(DateTime)
    last_billed_at = Column(DateTime, nullable=True)  # 上次计费时间点
    expired_at = Column(DateTime)
    release_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ai_user = relationship("AIUser", back_populates="instances")
    node = relationship("Node", back_populates="instances")
    image = relationship("Image")
    orders = relationship("Order", back_populates="instance")
    billing_records = relationship("BillingRecord", back_populates="instance")


class ImageType(str, enum.Enum):
    OFFICIAL = "official"
    COMMUNITY = "community"
    CUSTOM = "custom"


class ImageStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class Image(Base):
    __tablename__ = "images"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    version = Column(String(50), nullable=False)
    type = Column(Enum(ImageType), default=ImageType.OFFICIAL)
    size = Column(Float)  # GB
    description = Column(Text)
    is_public = Column(Boolean, default=True)
    author = Column(String(100))
    tags = Column(Text)  # JSON string
    supported_models = Column(Text)  # JSON string
    status = Column(Enum(ImageStatus), default=ImageStatus.ACTIVE)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OrderType(str, enum.Enum):
    CREATE = "create"
    RENEW = "renew"
    UPGRADE = "upgrade"
    RECHARGE = "recharge"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False)
    instance_id = Column(UUID(as_uuid=True), ForeignKey("instances.id"), nullable=True)
    openclaw_instance_id = Column(UUID(as_uuid=True), ForeignKey("openclaw_instances.id"), nullable=True)
    type = Column(Enum(OrderType), nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    description = Column(Text, nullable=True)      # 订单描述
    product_name = Column(String(100), nullable=True)  # 产品名称
    billing_cycle = Column(String(20), nullable=True)  # 计费周期: hourly/daily/monthly/yearly
    paid_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    ai_user = relationship("AIUser", back_populates="orders")
    instance = relationship("Instance", back_populates="orders")


class BillingRecord(Base):
    """按量计费流水（系统自动生成，与订单分离）"""
    __tablename__ = "billing_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False, index=True)
    instance_id = Column(UUID(as_uuid=True), ForeignKey("instances.id"), nullable=True)
    openclaw_instance_id = Column(UUID(as_uuid=True), ForeignKey("openclaw_instances.id"), nullable=True)

    amount = Column(Float, nullable=False)            # 扣费金额（正数）
    hourly_price = Column(Float, nullable=False)      # 当时的小时单价
    duration_seconds = Column(Integer, nullable=False) # 实际运行秒数
    period_start = Column(DateTime, nullable=False)    # 计费区间起点
    period_end = Column(DateTime, nullable=False)      # 计费区间终点
    description = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("AIUser")
    instance = relationship("Instance", back_populates="billing_records")


class PaymentMethod(str, enum.Enum):
    WECHAT = "wechat"
    ALIPAY = "alipay"
    BANK = "bank"


class RechargeStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class Recharge(Base):
    __tablename__ = "recharges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False)
    amount = Column(Float, nullable=False)
    payment_method = Column(Enum(PaymentMethod), nullable=False)
    transaction_id = Column(String(100))
    status = Column(Enum(RechargeStatus), default=RechargeStatus.PENDING)
    paid_at = Column(DateTime, nullable=True)  # 实际支付时间
    created_at = Column(DateTime, default=datetime.utcnow)

    ai_user = relationship("AIUser", back_populates="recharges")


class Storage(Base):
    __tablename__ = "storages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False)
    cluster_id = Column(UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=False)
    name = Column(String(255), nullable=False)
    size = Column(Float)  # bytes
    path = Column(String(500), nullable=False)
    is_directory = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserFile(Base):
    """用户文件表 - 虚拟目录树 + 存储后端映射"""
    __tablename__ = "user_files"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False, index=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("user_files.id"), nullable=True, index=True)

    name = Column(String(255), nullable=False)
    path = Column(String(1000), nullable=False)  # 完整路径, 如 /datasets/train/
    is_dir = Column(Boolean, default=False)

    size = Column(BigInteger, default=0)       # 文件大小(字节), 目录为0
    mime_type = Column(String(100))            # MIME类型
    storage_backend = Column(String(20), default="ipfs")  # ipfs / cos / rustfs / local
    storage_key = Column(String(500))          # IPFS CID / COS key / 文件路径

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    owner = relationship("AIUser", back_populates="files")
    parent = relationship("UserFile", remote_side="UserFile.id", backref="children")

    __table_args__ = (
        UniqueConstraint('user_id', 'parent_id', 'name', name='uq_user_parent_name'),
        # PG 中 NULL!=NULL, 需要 partial index 保证根目录下不重名
        Index('uq_user_root_name', 'user_id', 'name', unique=True, postgresql_where=text('parent_id IS NULL')),
    )


class AppImageStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class AppImage(Base):
    """应用镜像表 - 存储镜像配置信息（JSON格式）"""
    __tablename__ = "app_images"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)  # 镜像名称
    tag = Column(String(50), nullable=False)  # 镜像标签/版本
    category = Column(String(50), default="base")  # 分类：base/framework/model/tool
    description = Column(Text)  # 描述
    icon = Column(String(500))  # 图标URL
    image_url = Column(String(500))  # Docker镜像地址
    size_gb = Column(Float, default=0)  # 镜像大小
    config = Column(Text)  # JSON配置: {"ports": [], "envs": {}, "volumes": [], "commands": []}
    status = Column(Enum(AppImageStatus), default=AppImageStatus.ACTIVE)
    is_public = Column(Boolean, default=True)  # 是否公开
    sort_order = Column(Integer, default=0)  # 排序
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========== 工单系统 ==========
class TicketStatus(str, enum.Enum):
    OPEN = "open"           # 待处理
    PROCESSING = "processing"  # 处理中
    RESOLVED = "resolved"   # 已解决
    CLOSED = "closed"       # 已关闭


class TicketPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TicketCategory(str, enum.Enum):
    TECHNICAL = "technical"   # 技术问题
    BILLING = "billing"       # 计费问题
    ACCOUNT = "account"       # 账户问题
    SUGGESTION = "suggestion" # 建议反馈
    OTHER = "other"           # 其他


class Ticket(Base):
    """工单表"""
    __tablename__ = "tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    category = Column(Enum(TicketCategory), default=TicketCategory.OTHER)
    priority = Column(Enum(TicketPriority), default=TicketPriority.MEDIUM)
    status = Column(Enum(TicketStatus), default=TicketStatus.OPEN)
    handler_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=True)
    reply = Column(Text)
    replied_at = Column(DateTime)
    resolved_at = Column(DateTime)
    closed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("AIUser", foreign_keys=[user_id])
    handler = relationship("AIUser", foreign_keys=[handler_id])


class SystemSetting(Base):
    """系统设置表 - 键值对存储"""
    __tablename__ = "system_settings"

    key = Column(String(100), primary_key=True)  # 设置键名
    value = Column(Text)  # 设置值 (JSON字符串)
    description = Column(String(255))  # 描述
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========== OpenClaw 实例管理 ==========

class OpenClawInstance(Base):
    """OpenClaw AI Agent 实例表"""
    __tablename__ = "openclaw_instances"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    status = Column(String(20), default="creating")  # creating/running/stopped/error/releasing/released

    # K8s 调度
    namespace = Column(String(63))  # lmai-{user_id[:8]}
    node_name = Column(String(200))  # 指定调度节点
    node_type = Column(String(20), default="center")  # center / edge

    # 资源规格
    cpu_cores = Column(Integer, default=2)
    memory_gb = Column(Integer, default=4)
    disk_gb = Column(Integer, default=20)

    # 镜像 & 端口
    image_url = Column(String(500))  # Docker 镜像地址
    port = Column(Integer, default=18789)  # Gateway 端口

    # K8s 资源名
    deployment_name = Column(String(200))
    service_name = Column(String(200))

    # 连接信息
    internal_ip = Column(String(50))
    gateway_token = Column(String(200))  # Gateway Bearer Token

    # 生命周期
    started_at = Column(DateTime)
    last_billed_at = Column(DateTime, nullable=True)  # 上次计费时间点
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    user = relationship("AIUser")
    model_keys = relationship("ModelKey", back_populates="instance", cascade="all, delete-orphan")
    channels = relationship("Channel", back_populates="instance", cascade="all, delete-orphan")
    skills = relationship("OpenClawSkill", back_populates="instance", cascade="all, delete-orphan")


class ModelKey(Base):
    """大模型 API 密钥表"""
    __tablename__ = "openclaw_model_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_id = Column(UUID(as_uuid=True), ForeignKey("openclaw_instances.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(50), nullable=False)  # openai/anthropic/deepseek/qwen/...
    alias = Column(String(100))  # 用户自定义别名
    api_key = Column(String(500), nullable=False)  # API Key（生产环境应加密）
    base_url = Column(String(300))  # 自定义 endpoint（兼容 OpenAI 协议的第三方）
    model_name = Column(String(100))  # 默认模型名
    is_active = Column(Boolean, default=True)

    # 监控字段（由 ARQ 定时任务回写）
    last_check_at = Column(DateTime)
    check_status = Column(String(20), default="unknown")  # ok/error/quota_low/unknown
    balance = Column(Float)  # 余额（如果 provider 支持查询）
    tokens_used = Column(BigInteger, default=0)  # 累计 Token 消耗

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    instance = relationship("OpenClawInstance", back_populates="model_keys")


class Channel(Base):
    """消息通道配置表"""
    __tablename__ = "openclaw_channels"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_id = Column(UUID(as_uuid=True), ForeignKey("openclaw_instances.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(String(30), nullable=False)  # telegram/discord/wechat/feishu/dingtalk/whatsapp/qq
    name = Column(String(100))  # 显示名称
    config = Column(Text)  # JSON: {"token": "xxx", "webhook_url": "xxx", "app_id": "xxx", ...}
    is_active = Column(Boolean, default=True)

    # 监控字段
    online_status = Column(String(20), default="unknown")  # online/offline/error/unknown
    last_check_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    instance = relationship("OpenClawInstance", back_populates="channels")


class OpenClawSkill(Base):
    """OpenClaw Skills 技能表"""
    __tablename__ = "openclaw_skills"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_id = Column(UUID(as_uuid=True), ForeignKey("openclaw_instances.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)  # 技能名称（目录名）
    description = Column(Text)
    status = Column(String(20), default="installing")  # installed/installing/uninstalling/error
    version = Column(String(50))
    installed_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    instance = relationship("OpenClawInstance", back_populates="skills")


# ========== 资源套餐 ==========

class PlanType(str, enum.Enum):
    PACKAGE = "package"  # 固定套餐
    CUSTOM = "custom"    # 自定义规格单价


class BillingCycle(str, enum.Enum):
    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"
    YEARLY = "yearly"


class ResourcePlan(Base):
    """资源套餐表"""
    __tablename__ = "resource_plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    plan_type = Column(Enum(PlanType), default=PlanType.PACKAGE)
    billing_cycle = Column(Enum(BillingCycle), default=BillingCycle.MONTHLY)

    # 资源规格
    cpu_cores = Column(Integer, default=0)
    memory_gb = Column(Integer, default=0)
    gpu_count = Column(Integer, default=0)
    gpu_model = Column(String(100), nullable=True)
    disk_gb = Column(Integer, default=0)

    # 价格
    price = Column(Float, nullable=False)            # 单周期价格
    original_price = Column(Float, nullable=True)    # 原价（展示折扣用）

    # 状态
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ========== 积分系统 ==========

class PointType(str, enum.Enum):
    RECHARGE_REWARD = "recharge_reward"  # 充值奖励
    DAILY_LOGIN = "daily_login"          # 每日签到
    INVITE_REWARD = "invite_reward"      # 邀请奖励
    CONSUME = "consume"                  # 积分消费


class PointRecord(Base):
    """积分流水表"""
    __tablename__ = "point_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False, index=True)
    points = Column(Integer, nullable=False)  # 正数=获得, 负数=消费
    type = Column(Enum(PointType), nullable=False)
    description = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("AIUser", back_populates="point_records")


# ========== 操作日志 ==========

class AuditAction(str, enum.Enum):
    CREATE = "create"
    DELETE = "delete"
    UPDATE = "update"
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    LOGIN = "login"
    LOGOUT = "logout"
    REGISTER = "register"
    RECHARGE = "recharge"


class AuditResourceType(str, enum.Enum):
    INSTANCE = "instance"
    OPENCLAW = "openclaw"
    STORAGE = "storage"
    IMAGE = "image"
    ACCOUNT = "account"
    BILLING = "billing"


class AuditLog(Base):
    """操作日志表"""
    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False, index=True)
    action = Column(Enum(AuditAction), nullable=False)
    resource_type = Column(Enum(AuditResourceType), nullable=False)
    resource_id = Column(String(100), nullable=True)
    resource_name = Column(String(200), nullable=True)
    detail = Column(Text, nullable=True)  # JSON详情
    ip_address = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("AIUser")


# ========== 站内通知 ==========

class NotificationType(str, enum.Enum):
    SYSTEM = "system"
    BILLING = "billing"
    INSTANCE = "instance"
    POINTS = "points"


class Notification(Base):
    """站内通知表"""
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("ai_users.id"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=True)
    type = Column(Enum(NotificationType), default=NotificationType.SYSTEM)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("AIUser", back_populates="notifications")


# ========== 市场产品 ==========

class MarketCategory(str, enum.Enum):
    COMPUTE = "compute"     # 算力市场
    AI_APP = "ai_app"       # AI应用
    AI_SERVER = "ai_server" # AI服务器


class MarketProduct(Base):
    """市场产品表"""
    __tablename__ = "market_products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category = Column(Enum(MarketCategory), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    icon = Column(String(500), nullable=True)
    specs = Column(Text, nullable=True)  # JSON: {"gpu": "A100", "memory": "80GB", ...}
    price = Column(Float, default=0)
    price_unit = Column(String(50), default="元/小时")  # 价格单位
    tags = Column(Text, nullable=True)  # JSON: ["热门", "推荐"]
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PublicDataset(Base):
    """公开数据集表"""
    __tablename__ = "public_datasets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    category = Column(String(50), nullable=False, default="dataset")  # dataset/model/image/video/audio
    size = Column(String(50), nullable=True)       # "150GB"
    downloads = Column(Integer, default=0)
    description = Column(Text, nullable=True)
    tags = Column(JSON, default=list)  # ["图像分类","深度学习"]
    source = Column(String(100), nullable=True)     # "ModelScope"/"HuggingFace"
    source_url = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
