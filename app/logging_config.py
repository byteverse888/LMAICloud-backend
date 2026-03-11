"""
日志配置模块
支持文件滚动、大小限制、数量限制
"""
import os
import logging
from logging.handlers import RotatingFileHandler
from app.config import settings


def setup_logging() -> logging.Logger:
    """初始化日志系统"""
    # 创建日志目录
    log_dir = settings.log_dir
    if not os.path.isabs(log_dir):
        log_dir = os.path.join(os.getcwd(), log_dir)
    os.makedirs(log_dir, exist_ok=True)
    
    # 日志文件路径
    log_file = os.path.join(log_dir, "app.log")
    error_log_file = os.path.join(log_dir, "error.log")
    
    # 日志级别
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    
    # 日志格式
    formatter = logging.Formatter(settings.log_format)
    
    # 获取根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # 清除已有的 handlers（避免重复）
    root_logger.handlers.clear()
    
    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # 主日志文件（滚动）
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=settings.log_max_size_mb * 1024 * 1024,  # MB to bytes
        backupCount=settings.log_backup_count,
        encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # 错误日志文件（仅ERROR及以上）
    error_handler = RotatingFileHandler(
        error_log_file,
        maxBytes=settings.log_max_size_mb * 1024 * 1024,
        backupCount=settings.log_backup_count,
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)
    
    # 降低第三方库日志级别
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    
    # 返回应用日志器
    app_logger = logging.getLogger("lmaicloud")
    app_logger.info(f"日志系统初始化完成 - 日志目录: {log_dir}, 级别: {settings.log_level}")
    
    return app_logger


def get_logger(name: str = "lmaicloud") -> logging.Logger:
    """获取日志器"""
    return logging.getLogger(name)
