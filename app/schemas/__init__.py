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


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    user_id: Optional[str] = None


class UserResponse(BaseModel):
    id: UUID
    email: str
    nickname: Optional[str] = None
    avatar: Optional[str] = None
    role: str
    balance: float
    frozen_balance: float
    status: str
    verified: bool = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    user: UserResponse
    token: str


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

    # 资源类型与节点类型
    resource_type: str = "vGPU"       # vGPU / no_gpu
    node_type: str = "center"         # center / edge
    instance_count: int = 1

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
    ssh_host: Optional[str] = None
    ssh_port: Optional[int] = None
    ssh_password: Optional[str] = None
    internal_ip: Optional[str] = None
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
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Order Schemas
class OrderResponse(BaseModel):
    id: UUID
    user_id: UUID
    instance_id: Optional[UUID]
    type: str
    amount: float
    status: str
    paid_at: Optional[datetime]
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
    transaction_id: Optional[str]
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


# Instance Renew Schema
class InstanceRenew(BaseModel):
    duration_hours: int = 1  # 续费时长(小时)
    billing_type: str = "hourly"  # hourly/daily/weekly/monthly


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
    created_at: datetime


class FileListResponse(BaseModel):
    files: list
    total: int
    current_path: str


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

