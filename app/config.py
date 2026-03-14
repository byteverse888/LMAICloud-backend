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
    app_name: str = "LMAICloud"
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
    
    # GPU Node 默认配置
    default_gpu_hourly_price: float = 5.0  # 元/小时
    
    # Email SMTP 配置 (可通过 .env 或数据库动态配置覆盖)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "LMAICloud"
    smtp_use_tls: bool = True
    
    # 邮箱激活令牌过期时间（小时）
    email_activation_expire_hours: int = 24

    class Config:
        env_file = ".env"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
