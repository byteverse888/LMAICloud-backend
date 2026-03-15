"""
存储后端抽象层

支持 IPFS / Local / COS(预留) / RustFS(预留) 等多种存储后端，
对上层 API 提供统一的 upload / download_url / delete / exists 接口。
"""
import os
import uuid
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from app.config import settings
from app.logging_config import get_logger

logger = get_logger("lmaicloud.storage_provider")


# ========== 抽象接口 ==========

class StorageProvider(ABC):
    """存储后端抽象接口"""

    @abstractmethod
    async def upload(self, data: bytes, filename: str) -> str:
        """上传文件, 返回 storage_key (IPFS CID / COS key / 本地路径)"""

    @abstractmethod
    async def download_url(self, storage_key: str, filename: str, expires: int = 3600) -> str:
        """生成下载 URL (IPFS gateway / COS presigned URL / 本地文件路径)"""

    @abstractmethod
    async def delete(self, storage_key: str) -> bool:
        """删除文件"""

    @abstractmethod
    async def exists(self, storage_key: str) -> bool:
        """检查文件是否存在"""


# ========== IPFS 实现 ==========

class IpfsProvider(StorageProvider):
    """IPFS 存储后端"""

    def __init__(self, api_url: str, gateway_url: str):
        self.api_url = api_url.rstrip("/")
        self.gateway_url = gateway_url.rstrip("/")

    async def upload(self, data: bytes, filename: str) -> str:
        """上传文件到 IPFS, 返回 CID"""
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self.api_url}/api/v0/add",
                    files={"file": (filename, data)},
                )
                resp.raise_for_status()
                cid = resp.json()["Hash"]
                logger.info(f"IPFS upload OK: {filename} -> {cid} ({len(data)} bytes)")
                # pin 住防止 GC
                await client.post(f"{self.api_url}/api/v0/pin/add", params={"arg": cid})
                return cid
        except Exception as e:
            logger.error(f"IPFS upload failed: {filename}, error: {e}")
            raise

    async def download_url(self, storage_key: str, filename: str, expires: int = 3600) -> str:
        """返回 IPFS gateway 公开链接，边缘节点可直接 wget"""
        encoded_name = quote(filename)
        return f"{self.gateway_url}/ipfs/{storage_key}?filename={encoded_name}"

    async def delete(self, storage_key: str) -> bool:
        """IPFS unpin, 等待 GC 回收"""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.api_url}/api/v0/pin/rm",
                    params={"arg": storage_key},
                )
                logger.info(f"IPFS unpin: {storage_key}, status: {resp.status_code}")
                return True
        except Exception as e:
            logger.warning(f"IPFS unpin failed: {storage_key}, error: {e}")
            return False

    async def exists(self, storage_key: str) -> bool:
        """检查 IPFS 对象是否存在"""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.api_url}/api/v0/object/stat",
                    params={"arg": storage_key},
                )
                return resp.status_code == 200
        except Exception:
            return False


# ========== Local 实现 (开发/测试) ==========

class LocalProvider(StorageProvider):
    """本地文件系统存储后端（开发测试用）"""

    def __init__(self, root_dir: str):
        self.root = Path(root_dir) / "_user_files"
        self.root.mkdir(parents=True, exist_ok=True)

    def _key_path(self, storage_key: str) -> Path:
        return self.root / storage_key

    async def upload(self, data: bytes, filename: str) -> str:
        """写入本地文件, 返回随机 key"""
        key = f"{uuid.uuid4().hex[:12]}_{filename}"
        target = self._key_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        logger.info(f"Local upload: {filename} -> {key} ({len(data)} bytes)")
        return key

    async def download_url(self, storage_key: str, filename: str, expires: int = 3600) -> str:
        """返回本地下载路径（通过 API 中转, 返回 API 相对路径）"""
        # 本地模式下, 下载链接指向后端 API, 由 API 读取文件返回
        return f"/api/v1/storage/files/download-by-key?key={quote(storage_key)}&filename={quote(filename)}"

    async def delete(self, storage_key: str) -> bool:
        """删除本地文件"""
        target = self._key_path(storage_key)
        try:
            if target.exists():
                target.unlink()
                logger.info(f"Local delete: {storage_key}")
            return True
        except Exception as e:
            logger.warning(f"Local delete failed: {storage_key}, error: {e}")
            return False

    async def exists(self, storage_key: str) -> bool:
        return self._key_path(storage_key).exists()

    def get_file_path(self, storage_key: str) -> Optional[Path]:
        """获取本地文件绝对路径（用于 FileResponse）"""
        p = self._key_path(storage_key)
        return p if p.exists() else None


# ========== 工厂函数 ==========

_provider_instance: Optional[StorageProvider] = None


def get_storage_provider() -> StorageProvider:
    """获取存储后端单例"""
    global _provider_instance
    if _provider_instance is not None:
        return _provider_instance

    backend = settings.storage_backend.lower()
    if backend == "ipfs":
        _provider_instance = IpfsProvider(settings.ipfs_api_url, settings.ipfs_gateway_url)
    elif backend == "local":
        _provider_instance = LocalProvider(settings.storage_root)
    else:
        # 默认 local
        logger.warning(f"未知存储后端 '{backend}', 回退到 local")
        _provider_instance = LocalProvider(settings.storage_root)

    logger.info(f"存储后端初始化: {backend} -> {type(_provider_instance).__name__}")
    return _provider_instance
