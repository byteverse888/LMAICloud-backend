from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List
from uuid import UUID
from datetime import datetime


# Auth Schemas
class UserCreate(BaseModel):
    email: EmailStr
    username: Optional[str] = None
    nickname: Optional[str] = None
    password: str
    role: Optional[str] = "user"  # user 或 admin
    invite_code: Optional[str] = None  # 邀请码


class UserLogin(BaseModel):
    email: EmailStr
    password: str
    captcha_id: Optional[str] = None
    captcha_code: Optional[str] = None


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: Optional[str] = None


class UserResponse(BaseModel):
    id: UUID
    email: str
    nickname: Optional[str] = None
    phone: Optional[str] = None
    avatar: Optional[str] = None
    role: str
    balance: float
    frozen_balance: float
    points: int = 0
    invite_code: Optional[str] = None
    last_checkin_date: Optional[str] = None  # 最后签到日期 YYYY-MM-DD
    status: str
    verified: bool = False
    instance_quota: int = 20  # 实例配额(容器+OpenClaw总数上限)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    user: UserResponse
    token: str
    refresh_token: str


# Instance Schemas
class EnvVarItem(BaseModel):
    key: str
    value: str


class StorageMountItem(BaseModel):
    name: str = "data"
    mount_path: str = "/root/data"
    size_gb: int = 50


class InstanceCreate(BaseModel):
    """创建实例请求 - 完整版"""
    name: str
    node_id: str  # K8s 节点名称（不再是 DB UUID）
    gpu_count: int = 1
    gpu_model: Optional[str] = None
    image_id: Optional[UUID] = None
    image_url: Optional[str] = None  # 自定义/外部镜像地址
    billing_type: str = "hourly"
    duration_hours: Optional[int] = None

    # 规格配置（用户选择的 CPU/内存规格）
    cpu_cores: int = Field(default=2, ge=1, le=64, description="CPU 核数")
    memory_gb: int = Field(default=4, ge=1, le=256, description="内存 GB")
    spec_type: Optional[str] = None  # general / compute / memory
    spec_label: Optional[str] = None  # 规格标签，如 '2核4G'

    # 资源类型与节点类型
    resource_type: str = "vGPU"       # vGPU / no_gpu
    node_type: str = "center"         # center / edge
    instance_count: int = Field(default=1, ge=1, le=5)

    # 环境变量与存储
    env_vars: Optional[List[EnvVarItem]] = None
    storage_mounts: Optional[List[StorageMountItem]] = None

    # 启动命令
    startup_command: Optional[str] = None

    # 安装源
    pip_source: str = "default"
    conda_source: str = "default"
    apt_source: str = "default"

    # 自动关机/释放
    auto_shutdown_type: str = "none"          # none/timer/scheduled
    auto_shutdown_minutes: Optional[int] = None
    auto_shutdown_time: Optional[datetime] = None
    auto_release_type: str = "none"           # none/timer
    auto_release_minutes: Optional[int] = None


class ResourceConfigResponse(BaseModel):
    """可用资源配置（展示在资源表格中）"""
    node_id: str  # K8s 节点名称
    node_name: str
    node_type: str          # center / edge
    resource_type: str      # vGPU / no_gpu
    gpu_model: str
    gpu_memory: Optional[int] = None  # GB
    cpu_model: Optional[str] = None
    cpu_cores: Optional[int] = None
    memory: Optional[int] = None      # GB
    disk: Optional[int] = None        # GB
    disk_expandable: Optional[int] = None
    network_desc: Optional[str] = None
    gpu_available: int = 0
    gpu_total: int = 0
    hourly_price: float = 0.0
    region: Optional[str] = None

    class Config:
        from_attributes = True


class InstanceResponse(BaseModel):
    id: UUID
    user_id: UUID
    node_id: Optional[str] = None
    node_name: Optional[str] = None
    name: str

    @field_validator("node_id", mode="before")
    @classmethod
    def coerce_node_id(cls, v):
        if v is None:
            return None
        return str(v)

    status: str
    gpu_count: int
    gpu_model: Optional[str] = None
    cpu_cores: Optional[int] = None
    memory: Optional[int] = None
    disk: Optional[int] = None
    resource_type: Optional[str] = "vGPU"
    node_type: Optional[str] = "center"
    instance_count: Optional[int] = 1
    image_id: Optional[UUID] = None
    image_url: Optional[str] = None
    billing_type: str
    hourly_price: float
    internal_ip: Optional[str] = None
    namespace: Optional[str] = "lmaicloud"  # K8s 命名空间
    health_status: Optional[str] = "unknown"
    startup_command: Optional[str] = None
    env_vars: Optional[str] = None  # JSON string
    storage_mounts: Optional[str] = None  # JSON string
    pip_source: Optional[str] = "default"
    conda_source: Optional[str] = "default"
    apt_source: Optional[str] = "default"
    auto_shutdown_type: Optional[str] = "none"
    auto_shutdown_minutes: Optional[int] = None
    auto_release_type: Optional[str] = "none"
    auto_release_minutes: Optional[int] = None
    started_at: Optional[datetime] = None
    expired_at: Optional[datetime] = None
    release_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Cluster Schemas
class ClusterBase(BaseModel):
    name: Optional[str] = None
    region: Optional[str] = None
    description: Optional[str] = None


class ClusterCreate(BaseModel):
    name: str
    region: str
    description: Optional[str] = None


class ClusterResponse(BaseModel):
    id: UUID
    name: str
    region: str
    status: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Image Schemas
class ImageResponse(BaseModel):
    id: UUID
    name: str
    version: str
    type: str
    size: Optional[float]
    description: Optional[str]
    is_public: bool
    author: Optional[str]
    status: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Order Schemas
class OrderResponse(BaseModel):
    id: UUID
    user_id: UUID
    instance_id: Optional[UUID] = None
    openclaw_instance_id: Optional[UUID] = None
    type: str
    amount: float
    status: str
    description: Optional[str] = None
    product_name: Optional[str] = None
    billing_cycle: Optional[str] = None
    paid_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# Recharge Schemas
class RechargeCreate(BaseModel):
    amount: float
    payment_method: str


class RechargeResponse(BaseModel):
    id: UUID
    user_id: UUID
    amount: float
    payment_method: str
    transaction_id: Optional[str] = None
    status: str
    paid_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# Instance Renew Schema
class InstanceRenew(BaseModel):
    duration_hours: int = 1  # 续费时长(小时)
    billing_type: str = "hourly"  # hourly/daily/weekly/monthly


# Instance Rename Schema
class InstanceRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, description="新实例名称")


# Payment Schemas
class PaymentCreate(BaseModel):
    amount: float
    payment_method: str  # wechat/alipay


class PaymentResponse(BaseModel):
    order_id: str
    amount: float
    payment_method: str
    qr_code_url: Optional[str] = None  # 支付二维码URL
    pay_url: Optional[str] = None  # 支付跳转URL
    expire_time: datetime
    status: str


class PaymentCallbackData(BaseModel):
    order_id: str
    transaction_id: str
    status: str
    paid_at: Optional[datetime] = None


# Storage File Schemas
class FileUploadResponse(BaseModel):
    id: UUID
    name: str
    path: str
    size: int
    storage_backend: str = "ipfs"
    created_at: datetime


class FileItemResponse(BaseModel):
    id: UUID
    name: str
    path: str
    is_dir: bool
    size: int = 0
    mime_type: Optional[str] = None
    storage_backend: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class FileListResponse(BaseModel):
    files: List[FileItemResponse]
    total: int
    page: int
    page_size: int
    current_path: str


class StorageQuotaResponse(BaseModel):
    used: int       # 已用空间(字节)
    total: int      # 总配额(字节)
    remaining: int  # 剩余空间(字节)
    used_percent: float  # 使用百分比
    file_count: int = 0  # 当前文件/目录数
    max_file_count: int = 100  # 文件数上限
    max_upload_size: int = 50 * 1024 * 1024  # 单文件上传上限(字节)


class FileLinkResponse(BaseModel):
    url: str
    filename: str
    expires_in: int = 3600  # 秒


class MkdirRequest(BaseModel):
    path: str = "/"
    name: str


# Pagination
class PaginatedResponse(BaseModel):
    list: list
    total: int
    page: int
    size: int


# ========== 工单 Schemas ==========
class TicketCreate(BaseModel):
    title: str
    content: str
    category: str = "other"  # technical/billing/account/suggestion/other
    priority: str = "medium"  # low/medium/high/urgent


class TicketUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    priority: Optional[str] = None


class TicketReply(BaseModel):
    reply: str
    status: Optional[str] = None  # processing/resolved/closed


class TicketResponse(BaseModel):
    id: UUID
    user_id: UUID
    title: str
    content: str
    category: str
    priority: str
    status: str
    handler_id: Optional[UUID]
    reply: Optional[str]
    replied_at: Optional[datetime]
    resolved_at: Optional[datetime]
    closed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    # 关联用户信息
    user_email: Optional[str] = None
    user_nickname: Optional[str] = None
    handler_nickname: Optional[str] = None

    class Config:
        from_attributes = True


# ========== OpenClaw Schemas ==========

# -- 实例 --
class OpenClawInstanceCreate(BaseModel):
    name: str
    node_name: Optional[str] = None  # K8s 节点名；空则由调度器选择
    node_type: str = "center"  # center / edge
    cpu_cores: int = 2
    memory_gb: int = 4
    disk_gb: int = 20
    image_url: Optional[str] = None  # 空则使用默认 OpenClaw 镜像
    port: int = 18789
    # 计费
    billing_type: str = "hourly"  # hourly/monthly/yearly
    duration_months: Optional[int] = None  # 包月时长（1/3/6/12）
    # 创建时批量配置
    model_keys: Optional[List["ModelKeyCreate"]] = None
    channels: Optional[List["ChannelCreate"]] = None
    skills: Optional[List["SkillInstall"]] = None


class OpenClawInstanceResponse(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    status: str
    namespace: Optional[str] = None
    node_name: Optional[str] = None
    node_type: str = "center"
    cpu_cores: int = 2
    memory_gb: int = 4
    disk_gb: int = 20
    image_url: Optional[str] = None
    port: int = 18789
    # 计费
    billing_type: Optional[str] = "hourly"
    hourly_price: Optional[float] = 0.12
    expired_at: Optional[datetime] = None
    deployment_name: Optional[str] = None
    service_name: Optional[str] = None
    internal_ip: Optional[str] = None
    gateway_token: Optional[str] = None
    started_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class OpenClawSpecUpdate(BaseModel):
    cpu_cores: Optional[int] = None
    memory_gb: Optional[int] = None
    disk_gb: Optional[int] = None


# -- 大模型密钥 --
class ModelKeyCreate(BaseModel):
    model_config = {"protected_namespaces": ()}

    provider: str  # openai/anthropic/deepseek/qwen/...
    alias: Optional[str] = None
    api_key: str
    base_url: Optional[str] = None
    model_name: Optional[str] = None


class ModelKeyUpdate(BaseModel):
    model_config = {"protected_namespaces": ()}

    alias: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None
    is_active: Optional[bool] = None


class ModelKeyResponse(BaseModel):
    model_config = {"from_attributes": True, "protected_namespaces": ()}

    id: UUID
    instance_id: UUID
    provider: str
    alias: Optional[str] = None
    api_key_masked: str = ""  # 前端展示脱敏后的 key
    base_url: Optional[str] = None
    model_name: Optional[str] = None
    is_active: bool = True
    last_check_at: Optional[datetime] = None
    check_status: str = "unknown"
    balance: Optional[float] = None
    tokens_used: int = 0
    created_at: datetime


# -- 通道配置 --
class ChannelCreate(BaseModel):
    type: str  # telegram/discord/wechat/feishu/dingtalk/whatsapp/qq
    name: Optional[str] = None
    config: str  # JSON 字符串


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[str] = None
    is_active: Optional[bool] = None


class ChannelResponse(BaseModel):
    id: UUID
    instance_id: UUID
    type: str
    name: Optional[str] = None
    config: Optional[str] = None
    is_active: bool = True
    online_status: str = "unknown"
    last_check_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# -- Skills --
class SkillInstall(BaseModel):
    name: str
    version: Optional[str] = None
    description: Optional[str] = None


class SkillUpdate(BaseModel):
    version: Optional[str] = None
    description: Optional[str] = None


class SkillResponse(BaseModel):
    id: UUID
    instance_id: UUID
    name: str
    description: Optional[str] = None
    status: str = "installing"
    version: Optional[str] = None
    installed_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# -- 监控 --
class MonitorModelResponse(BaseModel):
    key_id: UUID
    provider: str
    alias: Optional[str] = None
    check_status: str = "unknown"
    balance: Optional[float] = None
    tokens_used: int = 0
    last_check_at: Optional[datetime] = None


class MonitorChannelResponse(BaseModel):
    channel_id: UUID
    type: str
    name: Optional[str] = None
    online_status: str = "unknown"
    last_check_at: Optional[datetime] = None


class MonitorStatusResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    instance_id: UUID
    status: str
    internal_ip: Optional[str] = None
    port: int = 18789
    gateway_version: Optional[str] = None
    uptime: Optional[int] = None
    session_count: Optional[int] = None
    model_keys_total: int = 0
    model_keys_ok: int = 0
    channels_total: int = 0
    channels_online: int = 0
    skills_installed: int = 0
    health: bool = False
    ready: bool = False
    # K8s 资源监控
    cpu_usage_millicores: Optional[int] = None
    memory_usage_bytes: Optional[int] = None
    cpu_cores: Optional[int] = None      # 实例配置的 CPU 核数
    memory_gb: Optional[int] = None      # 实例配置的内存 GB


# ========== 资源套餐 Schemas ==========

class ResourcePlanCreate(BaseModel):
    name: str
    description: Optional[str] = None
    plan_type: str = "package"  # package / custom
    billing_cycle: str = "monthly"  # hourly/daily/monthly/yearly
    cpu_cores: int = 0
    memory_gb: int = 0
    gpu_count: int = 0
    gpu_model: Optional[str] = None
    disk_gb: int = 0
    price: float
    original_price: Optional[float] = None
    is_active: bool = True
    sort_order: int = 0


class ResourcePlanUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    plan_type: Optional[str] = None
    billing_cycle: Optional[str] = None
    cpu_cores: Optional[int] = None
    memory_gb: Optional[int] = None
    gpu_count: Optional[int] = None
    gpu_model: Optional[str] = None
    disk_gb: Optional[int] = None
    price: Optional[float] = None
    original_price: Optional[float] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class ResourcePlanResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    plan_type: str
    billing_cycle: str
    cpu_cores: int
    memory_gb: int
    gpu_count: int
    gpu_model: Optional[str] = None
    disk_gb: int
    price: float
    original_price: Optional[float] = None
    is_active: bool
    sort_order: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ========== 积分 Schemas ==========

class PointRecordResponse(BaseModel):
    id: UUID
    user_id: UUID
    points: int
    type: str
    description: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ========== 操作日志 Schemas ==========

class AuditLogResponse(BaseModel):
    id: UUID
    user_id: UUID
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    detail: Optional[str] = None
    ip_address: Optional[str] = None
    user_email: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ========== 通知 Schemas ==========

class NotificationResponse(BaseModel):
    id: UUID
    user_id: UUID
    title: str
    content: Optional[str] = None
    type: str
    is_read: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


# ========== 市场产品 Schemas ==========

class MarketProductCreate(BaseModel):
    category: str  # compute / ai_app / ai_server
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    specs: Optional[str] = None  # JSON string
    price: float = 0
    price_unit: str = "元/小时"
    tags: Optional[str] = None  # JSON string
    sort_order: int = 0
    is_active: bool = True


class MarketProductUpdate(BaseModel):
    category: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    specs: Optional[str] = None
    price: Optional[float] = None
    price_unit: Optional[str] = None
    tags: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class MarketProductResponse(BaseModel):
    id: UUID
    category: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    specs: Optional[str] = None
    price: float
    price_unit: str = "元/小时"
    tags: Optional[str] = None
    sort_order: int
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ========== 公开数据集 Schemas ==========

class PublicDatasetCreate(BaseModel):
    name: str
    category: str = "dataset"  # dataset/model/image/video/audio
    size: Optional[str] = None
    downloads: int = 0
    description: Optional[str] = None
    tags: Optional[list] = []
    source: Optional[str] = None
    source_url: Optional[str] = None
    is_active: bool = True
    sort_order: int = 0


class PublicDatasetUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    size: Optional[str] = None
    downloads: Optional[int] = None
    description: Optional[str] = None
    tags: Optional[list] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class PublicDatasetResponse(BaseModel):
    id: UUID
    name: str
    category: str
    size: Optional[str] = None
    downloads: int = 0
    description: Optional[str] = None
    tags: Optional[list] = []
    source: Optional[str] = None
    source_url: Optional[str] = None
    is_active: bool = True
    sort_order: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

