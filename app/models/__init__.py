import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, DateTime, Enum, Boolean, ForeignKey, Integer, Text
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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    instances = relationship("Instance", back_populates="ai_user")
    orders = relationship("Order", back_populates="ai_user")
    recharges = relationship("Recharge", back_populates="ai_user")


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
    expired_at = Column(DateTime)
    release_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ai_user = relationship("AIUser", back_populates="instances")
    node = relationship("Node", back_populates="instances")
    image = relationship("Image")
    orders = relationship("Order", back_populates="instance")


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
    type = Column(Enum(OrderType), nullable=False)
    amount = Column(Float, nullable=False)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    paid_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    ai_user = relationship("AIUser", back_populates="orders")
    instance = relationship("Instance", back_populates="orders")


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
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
