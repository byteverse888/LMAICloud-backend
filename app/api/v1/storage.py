"""
存储管理 API

提供文件上传(IPFS)、目录管理、分页列表、下载链接、删除等功能。
存储后端通过 StorageProvider 抽象层支持 IPFS / COS / Local 等。
"""
import mimetypes
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func

from app.database import get_db
from app.models import User, UserFile
from app.schemas import (
    FileUploadResponse, FileItemResponse, FileListResponse,
    StorageQuotaResponse, FileLinkResponse, MkdirRequest,
)
from app.utils.auth import get_current_user
from app.config import settings
from app.services.storage_provider import get_storage_provider, LocalProvider
from app.logging_config import get_logger

router = APIRouter()
logger = get_logger("lmaicloud.storage")

MAX_UPLOAD_SIZE = settings.user_upload_max_size_mb * 1024 * 1024  # 字节
MAX_FILE_COUNT = settings.user_max_file_count  # 每用户文件/目录数上限


# ========== 辅助函数 ==========

def _parent_filter(parent_id):
    """生成 parent_id 过滤条件, 正确处理 NULL"""
    if parent_id is None:
        return UserFile.parent_id.is_(None)
    return UserFile.parent_id == parent_id


async def _check_file_count(db: AsyncSession, user_id) -> int:
    """检查用户文件总数, 返回当前数量"""
    result = await db.execute(
        select(func.count()).select_from(UserFile).where(UserFile.user_id == user_id)
    )
    return result.scalar() or 0

async def _resolve_parent(db: AsyncSession, user_id, path: str) -> Optional[UUID]:
    """根据路径解析出 parent_id, 根目录返回 None"""
    path = path.strip("/")
    if not path:
        return None  # 根目录

    parts = path.split("/")
    parent_id = None
    for part in parts:
        result = await db.execute(
            select(UserFile).where(
                UserFile.user_id == user_id,
                _parent_filter(parent_id),
                UserFile.name == part,
                UserFile.is_dir == True,
            )
        )
        folder = result.scalar_one_or_none()
        if not folder:
            return "NOT_FOUND"  # 路径不存在
        parent_id = folder.id
    return parent_id


async def _collect_subtree_files(db: AsyncSession, user_id, dir_id: UUID) -> list:
    """递归收集目录下所有非目录文件 (用于删除和配额回收)"""
    files = []
    # 获取直接子项
    result = await db.execute(
        select(UserFile).where(
            UserFile.user_id == user_id,
            UserFile.parent_id == dir_id,
        )
    )
    children = result.scalars().all()
    for child in children:
        if child.is_dir:
            files.extend(await _collect_subtree_files(db, user_id, child.id))
        else:
            files.append(child)
    return files


# ========== API 端点 ==========

@router.get("/quota", response_model=StorageQuotaResponse)
async def get_storage_quota(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户存储配额"""
    used = current_user.storage_used or 0
    total = current_user.storage_quota or (settings.user_storage_default_quota_gb * 1024**3)
    remaining = max(0, total - used)
    used_percent = round((used / total * 100), 2) if total > 0 else 0

    # 文件计数
    count_result = await db.execute(
        select(func.count()).select_from(UserFile).where(UserFile.user_id == current_user.id)
    )
    file_count = count_result.scalar() or 0

    return StorageQuotaResponse(
        used=used,
        total=total,
        remaining=remaining,
        used_percent=used_percent,
        file_count=file_count,
        max_file_count=settings.user_max_file_count,
        max_upload_size=MAX_UPLOAD_SIZE,
    )


@router.get("/files", response_model=FileListResponse)
async def list_files(
    path: str = Query("/", description="目录路径"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出目录下的文件(分页)"""
    # 解析目标目录
    parent_id = await _resolve_parent(db, current_user.id, path)
    if parent_id == "NOT_FOUND":
        return FileListResponse(files=[], total=0, page=page, page_size=page_size, current_path=path)

    # 计数
    count_q = select(func.count()).select_from(UserFile).where(
        UserFile.user_id == current_user.id,
        _parent_filter(parent_id),
    )
    total_result = await db.execute(count_q)
    total = total_result.scalar() or 0

    # 分页查询: 目录优先, 名称升序
    offset = (page - 1) * page_size
    query = (
        select(UserFile)
        .where(
            UserFile.user_id == current_user.id,
            _parent_filter(parent_id),
        )
        .order_by(UserFile.is_dir.desc(), UserFile.name.asc())
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(query)
    items = result.scalars().all()

    files = [
        FileItemResponse(
            id=item.id,
            name=item.name,
            path=item.path,
            is_dir=item.is_dir,
            size=item.size or 0,
            mime_type=item.mime_type,
            storage_backend=item.storage_backend,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
        for item in items
    ]

    return FileListResponse(
        files=files,
        total=total,
        page=page,
        page_size=page_size,
        current_path=path,
    )


@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    path: str = Form("/"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上传文件

    - 单文件不超过 50MB
    - 配额不足时拒绝
    - 文件数超限时拒绝
    - 文件存储到 IPFS, DB 记录元数据
    """
    # 0. 文件名校验
    filename = (file.filename or "").strip()
    if not filename:
        await file.close()
        raise HTTPException(status_code=400, detail="文件名为空")

    # 1. 读取文件内容并校验大小
    try:
        data = await file.read()
    finally:
        await file.close()  # 立即关闭底层 SpooledTemporaryFile, 释放 /tmp 临时文件
    file_size = len(data)

    if file_size > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"文件过大, 单文件最大允许 {settings.user_upload_max_size_mb}MB"
        )

    if file_size == 0:
        raise HTTPException(status_code=400, detail="文件为空")

    # 1.5 文件数上限检查
    file_count = await _check_file_count(db, current_user.id)
    if file_count >= MAX_FILE_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"文件数量已达上限({MAX_FILE_COUNT}个), 请删除不需要的文件后重试"
        )

    # 2. 原子配额检查+扣减
    quota_result = await db.execute(
        update(User)
        .where(
            User.id == current_user.id,
            User.storage_used + file_size <= User.storage_quota,
        )
        .values(storage_used=User.storage_used + file_size)
        .returning(User.storage_used)
    )
    new_used = quota_result.scalar_one_or_none()
    if new_used is None:
        await db.rollback()
        raise HTTPException(status_code=400, detail="存储空间不足, 请清理文件或申请扩容")
    await db.flush()

    # 3. 解析目标目录
    parent_id = await _resolve_parent(db, current_user.id, path)
    if parent_id == "NOT_FOUND":
        # 配额回滚
        await db.execute(
            update(User).where(User.id == current_user.id)
            .values(storage_used=User.storage_used - file_size)
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=f"目录 {path} 不存在")

    # 4. 上传到存储后端
    provider = get_storage_provider()
    try:
        storage_key = await provider.upload(data, filename)
    except Exception as e:
        # 上传失败, 配额回滚
        await db.execute(
            update(User).where(User.id == current_user.id)
            .values(storage_used=User.storage_used - file_size)
        )
        await db.commit()
        logger.error(f"存储后端上传失败: {e}")
        raise HTTPException(status_code=500, detail="文件上传失败, 请稍后重试")

    # 5. 写入 DB
    full_path = ("/" + path.strip("/") + "/" + filename).replace("//", "/")
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    user_file = UserFile(
        user_id=current_user.id,
        parent_id=parent_id,
        name=filename,
        path=full_path,
        is_dir=False,
        size=file_size,
        mime_type=mime,
        storage_backend=settings.storage_backend,
        storage_key=storage_key,
    )
    db.add(user_file)

    try:
        await db.commit()
        await db.refresh(user_file)
    except Exception as e:
        # DB 写入失败(可能是同名文件), 尝试删除已上传的文件 + 配额回滚
        await provider.delete(storage_key)
        await db.rollback()
        await db.execute(
            update(User).where(User.id == current_user.id)
            .values(storage_used=User.storage_used - file_size)
        )
        await db.commit()
        # 判断是否为重复文件名
        if "uq_user_parent_name" in str(e) or "uq_user_root_name" in str(e):
            raise HTTPException(status_code=400, detail=f"同名文件已存在: {filename}")
        logger.error(f"DB 写入失败: {e}")
        raise HTTPException(status_code=500, detail="文件记录保存失败")

    logger.info(f"文件上传成功: user={current_user.id}, file={filename}, size={file_size}, key={storage_key}")

    return FileUploadResponse(
        id=user_file.id,
        name=user_file.name,
        path=user_file.path,
        size=file_size,
        storage_backend=user_file.storage_backend,
        created_at=user_file.created_at,
    )


@router.post("/mkdir")
async def create_directory(
    body: MkdirRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建目录"""
    name = body.name.strip()
    if not name or "/" in name:
        raise HTTPException(status_code=400, detail="无效的目录名称")

    # 文件数上限检查
    file_count = await _check_file_count(db, current_user.id)
    if file_count >= MAX_FILE_COUNT:
        raise HTTPException(
            status_code=400,
            detail=f"文件数量已达上限({MAX_FILE_COUNT}个), 请删除不需要的文件后重试"
        )

    # 解析父目录
    parent_id = await _resolve_parent(db, current_user.id, body.path)
    if parent_id == "NOT_FOUND":
        raise HTTPException(status_code=400, detail=f"父目录 {body.path} 不存在")

    # 检查同名
    existing = await db.execute(
        select(UserFile).where(
            UserFile.user_id == current_user.id,
            _parent_filter(parent_id),
            UserFile.name == name,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"已存在同名文件或目录: {name}")

    full_path = ("/" + body.path.strip("/") + "/" + name).replace("//", "/")

    folder = UserFile(
        user_id=current_user.id,
        parent_id=parent_id,
        name=name,
        path=full_path,
        is_dir=True,
        size=0,
        storage_backend=settings.storage_backend,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)

    return {
        "id": str(folder.id),
        "name": folder.name,
        "path": folder.path,
        "message": "目录创建成功",
    }


@router.get("/files/{file_id}/link", response_model=FileLinkResponse)
async def get_file_link(
    file_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取文件下载链接 (用于 wget 等工具直接下载)"""
    result = await db.execute(
        select(UserFile).where(
            UserFile.id == file_id,
            UserFile.user_id == current_user.id,
            UserFile.is_dir == False,
        )
    )
    file_record = result.scalar_one_or_none()
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")

    if not file_record.storage_key:
        raise HTTPException(status_code=400, detail="文件存储信息缺失")

    provider = get_storage_provider()
    url = await provider.download_url(file_record.storage_key, file_record.name)

    return FileLinkResponse(
        url=url,
        filename=file_record.name,
        expires_in=3600,
    )


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除文件或目录

    - 删除目录时级联删除所有子文件
    - 回收配额
    """
    result = await db.execute(
        select(UserFile).where(
            UserFile.id == file_id,
            UserFile.user_id == current_user.id,
        )
    )
    file_record = result.scalar_one_or_none()
    if not file_record:
        raise HTTPException(status_code=404, detail="文件不存在")

    provider = get_storage_provider()
    total_freed = 0

    if file_record.is_dir:
        # 递归收集所有子文件
        sub_files = await _collect_subtree_files(db, current_user.id, file_record.id)
        for f in sub_files:
            total_freed += (f.size or 0)
            if f.storage_key:
                try:
                    await provider.delete(f.storage_key)
                except Exception as e:
                    logger.warning(f"删除存储文件失败: key={f.storage_key}, err={e}")
        # 级联删除(DB ON DELETE CASCADE 会处理子记录)
    else:
        total_freed = file_record.size or 0
        if file_record.storage_key:
            try:
                await provider.delete(file_record.storage_key)
            except Exception as e:
                logger.warning(f"删除存储文件失败: key={file_record.storage_key}, err={e}")

    # 删除 DB 记录
    await db.delete(file_record)

    # 原子回收配额
    if total_freed > 0:
        await db.execute(
            update(User).where(User.id == current_user.id)
            .values(storage_used=func.greatest(User.storage_used - total_freed, 0))
        )

    await db.commit()

    logger.info(f"文件删除: user={current_user.id}, file={file_record.name}, freed={total_freed}")

    return {"message": "删除成功", "id": file_id, "freed_bytes": total_freed}


# ========== 兼容: 旧版列表接口 ==========

@router.get("")
async def list_storage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取存储列表(兼容旧接口, 返回根目录文件)"""
    result = await db.execute(
        select(UserFile).where(
            UserFile.user_id == current_user.id,
            UserFile.parent_id.is_(None),
        ).order_by(UserFile.is_dir.desc(), UserFile.name.asc())
        .limit(100)
    )
    items = result.scalars().all()

    return {
        "list": [
            {
                "id": str(item.id),
                "name": item.name,
                "size": item.size or 0,
                "path": item.path,
                "is_directory": item.is_dir,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in items
        ],
        "total": len(items),
        "page": 1,
        "size": 100,
    }

