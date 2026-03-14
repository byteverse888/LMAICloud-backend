"""
存储管理 API

提供文件上传、下载、列表、删除等功能
"""
import os
import uuid
import aiofiles
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, Storage, Cluster
from app.utils.auth import get_current_user
from app.config import settings
from app.logging_config import get_logger

router = APIRouter()
logger = get_logger("lmaicloud.storage")

# 存储根目录（从 settings 读取，.env 中 STORAGE_ROOT 配置）
STORAGE_ROOT = settings.storage_root


def get_user_storage_path(user_id: str, region: str) -> Path:
    """获取用户存储路径（自动创建目录）"""
    try:
        path = Path(STORAGE_ROOT) / region / user_id
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception as e:
        logger.warning(f"创建存储目录失败: {e}")
        # 回退到临时路径，保证不抛异常
        fallback = Path("/tmp/lmaicloud_storage") / region / user_id
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


@router.get("")
async def list_storage(
    region: str = "beijing-b",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取存储列表(顶层目录)"""
    try:
        result = await db.execute(
            select(Storage).where(
                Storage.user_id == current_user.id
            )
        )
        storages = result.scalars().all()

        return {
            "list": [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "size": s.size,
                    "path": s.path,
                    "is_directory": s.is_directory,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in storages
            ],
            "total": len(storages),
            "page": 1,
            "size": 20
        }
    except Exception as e:
        logger.error(f"list_storage 异常: {e}")
        return {
            "list": [],
            "total": 0,
            "page": 1,
            "size": 20
        }


@router.get("/files")
async def list_files(
    region: str = "beijing-b",
    path: str = "/",
    current_user: User = Depends(get_current_user)
):
    """列出目录下的文件"""
    try:
        user_path = get_user_storage_path(str(current_user.id), region)
        target_path = user_path / path.lstrip("/")

        if not target_path.exists():
            target_path.mkdir(parents=True, exist_ok=True)

        files = []
        try:
            for item in target_path.iterdir():
                stat = item.stat()
                files.append({
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, str(item))),
                    "name": item.name,
                    "path": str(item.relative_to(user_path)),
                    "size": stat.st_size if item.is_file() else 0,
                    "size_formatted": format_size(stat.st_size) if item.is_file() else "-",
                    "is_directory": item.is_dir(),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
        except Exception as e:
            logger.warning(f"遍历目录失败 {target_path}: {e}")

        # 按目录优先、名称排序
        files.sort(key=lambda x: (not x["is_directory"], x["name"].lower()))

        return {
            "files": files,
            "total": len(files),
            "current_path": path
        }
    except Exception as e:
        logger.error(f"list_files 异常: {e}")
        return {
            "files": [],
            "total": 0,
            "current_path": path
        }


@router.get("/quota")
async def get_storage_quota(
    region: str = "beijing-b",
    current_user: User = Depends(get_current_user)
):
    """获取存储配额"""
    # 默认配额
    free_quota = 20 * 1024 * 1024 * 1024  # 20GB免费
    default_total = 200 * 1024 * 1024 * 1024  # 200GB总配额

    total_size = 0
    try:
        user_path = get_user_storage_path(str(current_user.id), region)
        for item in user_path.rglob("*"):
            if item.is_file():
                total_size += item.stat().st_size
    except Exception as e:
        logger.warning(f"计算存储用量失败: {e}")

    return {
        "used": total_size,
        "total": default_total,
        "free": free_quota,
        "paid": 0
    }


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    region: str = Form("beijing-b"),
    path: str = Form("/"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """上传文件"""
    user_path = get_user_storage_path(str(current_user.id), region)
    target_dir = user_path / path.lstrip("/")
    target_dir.mkdir(parents=True, exist_ok=True)
    
    target_file = target_dir / file.filename
    
    # 检查文件大小(限制单文件5GB)
    MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024
    
    # 写入文件
    total_size = 0
    async with aiofiles.open(target_file, 'wb') as f:
        while chunk := await file.read(1024 * 1024):  # 1MB chunks
            total_size += len(chunk)
            if total_size > MAX_FILE_SIZE:
                await f.close()
                target_file.unlink()
                raise HTTPException(status_code=400, detail="File too large (max 5GB)")
            await f.write(chunk)
    
    # 记录到数据库
    storage_record = Storage(
        user_id=current_user.id,
        cluster_id=uuid.UUID('00000000-0000-0000-0000-000000000001'),  # 默认集群
        name=file.filename,
        size=total_size,
        path=str(target_file.relative_to(user_path)),
        is_directory=False
    )
    db.add(storage_record)
    await db.commit()
    await db.refresh(storage_record)
    
    return {
        "id": str(storage_record.id),
        "name": file.filename,
        "path": str(target_file.relative_to(user_path)),
        "size": total_size,
        "created_at": storage_record.created_at.isoformat()
    }


@router.post("/mkdir")
async def create_directory(
    region: str = "beijing-b",
    path: str = "/",
    name: str = "new_folder",
    current_user: User = Depends(get_current_user)
):
    """创建目录"""
    user_path = get_user_storage_path(str(current_user.id), region)
    target_dir = user_path / path.lstrip("/") / name
    
    if target_dir.exists():
        raise HTTPException(status_code=400, detail="Directory already exists")
    
    target_dir.mkdir(parents=True, exist_ok=True)
    
    return {
        "message": "Directory created",
        "path": str(target_dir.relative_to(user_path))
    }


@router.get("/files/{file_id}/download")
async def download_file(
    file_id: str,
    token: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """下载文件"""
    result = await db.execute(
        select(Storage).where(
            Storage.id == file_id,
            Storage.user_id == current_user.id
        )
    )
    storage = result.scalar_one_or_none()
    
    if not storage:
        raise HTTPException(status_code=404, detail="File not found")
    
    # 构建文件路径
    # 简化实现，实际应根据region查找
    for region in ["beijing-b", "beijing-a", "northwest-b"]:
        user_path = get_user_storage_path(str(current_user.id), region)
        file_path = user_path / storage.path
        if file_path.exists():
            return FileResponse(
                path=str(file_path),
                filename=storage.name,
                media_type="application/octet-stream"
            )
    
    raise HTTPException(status_code=404, detail="File not found on disk")


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """删除文件"""
    result = await db.execute(
        select(Storage).where(
            Storage.id == file_id,
            Storage.user_id == current_user.id
        )
    )
    storage = result.scalar_one_or_none()
    
    if not storage:
        raise HTTPException(status_code=404, detail="File not found")
    
    # 删除实际文件
    for region in ["beijing-b", "beijing-a", "northwest-b"]:
        user_path = get_user_storage_path(str(current_user.id), region)
        file_path = user_path / storage.path
        if file_path.exists():
            if file_path.is_dir():
                import shutil
                shutil.rmtree(file_path)
            else:
                file_path.unlink()
            break
    
    # 删除数据库记录
    await db.delete(storage)
    await db.commit()
    
    return {"message": "File deleted", "id": file_id}
