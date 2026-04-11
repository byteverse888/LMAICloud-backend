from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/lmaicloud"
    
    # JWT
    jwt_secret_key: str = "your-super-secret-key-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_minutes: int = 10
    
    # Redis
    redis_url: str = "redis://localhost:6379"
    
    # CORS
    cors_origins: str = "http://localhost:3000"
    
    # App
    app_name: str = "貔貅云"
    app_env: str = "development"
    debug: bool = True
    frontend_url: str = "http://localhost:3000"  # 前端地址，用于邮件中的链接
    
    # Logging
    log_level: str = "INFO"
    log_dir: str = "logs"
    log_max_size_mb: int = 10
    log_backup_count: int = 5
    log_format: str = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"
    
    # K8s Cluster
    k8s_cluster_name: str = "default-cluster"
    k8s_cluster_region: str = "cn-beijing"
    kubeconfig_path: str = ""  # 留空则使用默认路径 ~/.kube/config
    
    # 存储
    storage_root: str = "/opt/data/"
    storage_backend: str = "ipfs"  # ipfs / cos / rustfs / local
    ipfs_api_url: str = "http://127.0.0.1:5001"  # IPFS API 地址
    ipfs_gateway_url: str = "http://127.0.0.1:8080"  # IPFS Gateway (wget 下载用)
    user_storage_default_quota_gb: int = 10  # 默认用户存储配额(GB)
    user_upload_max_size_mb: int = 50  # 单文件上传上限(MB)
    user_max_file_count: int = 100  # 每用户最大文件/目录数
    user_default_instance_quota: int = 20  # 新用户默认实例配额(容器+OpenClaw总数)
    # COS 预留
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_bucket: str = ""
    cos_region: str = ""
    
    # GPU Node 默认配置
    default_gpu_hourly_price: float = 5.0  # 元/小时
    
    # Email SMTP 配置 (可通过 .env 或数据库动态配置覆盖)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = ""  # 空则自动使用品牌配置中的平台名称
    smtp_use_tls: bool = True
    
    # 邮箱激活令牌过期时间（小时）
    email_activation_expire_hours: int = 24
    
    # 微信支付 (V2)
    wechat_app_id: str = ""
    wechat_mch_id: str = ""
    wechat_api_key: str = ""  # V2 API密钥
    wechat_notify_url: str = ""
    wechat_test_mode: bool = True  # True=模拟支付, False=真实微信支付

    # OpenClaw
    openclaw_default_port: int = 18789
    openclaw_default_image: str = "ghcr.io/openclaw/openclaw:latest"
    openclaw_storage_class: str = "local-path"
    openclaw_edge_storage_class: str = "openclaw-edge-local"
    openclaw_edge_storage_path: str = "/opt/openclaw-data"
    # OpenClaw 规格价格表 (cpu_cores, memory_gb) -> hourly_price
    openclaw_spec_prices: str = '{"1_2": 0.06, "2_4": 0.12, "4_8": 0.24, "8_16": 0.48}'

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
