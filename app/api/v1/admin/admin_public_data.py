"""
管理端 - 公开数据集 CRUD API
"""
import logging
from uuid import UUID
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from app.database import get_db
from app.models import PublicDataset
from app.schemas import (
    PaginatedResponse,
    PublicDatasetCreate,
    PublicDatasetUpdate,
    PublicDatasetResponse,
)
from app.utils.auth import get_current_admin_user

router = APIRouter()
logger = logging.getLogger("lmaicloud.admin.public_data")


@router.get("", response_model=PaginatedResponse, summary="管理端-公开数据集列表")
async def list_datasets(
    page: int = 1,
    size: int = 20,
    category: Optional[str] = None,
    search: Optional[str] = None,
    is_active: Optional[bool] = None,
    _admin=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(PublicDataset)

    if category and category != "all":
        query = query.where(PublicDataset.category == category)
    if is_active is not None:
        query = query.where(PublicDataset.is_active == is_active)
    if search:
        keyword = f"%{search}%"
        query = query.where(
            or_(
                PublicDataset.name.ilike(keyword),
                PublicDataset.description.ilike(keyword),
            )
        )

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(PublicDataset.sort_order, PublicDataset.created_at.desc())
    query = query.offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    items = result.scalars().all()

    return PaginatedResponse(
        list=[PublicDatasetResponse.model_validate(i) for i in items],
        total=total,
        page=page,
        size=size,
    )


@router.post("", summary="新增公开数据集")
async def create_dataset(
    data: PublicDatasetCreate,
    _admin=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    item = PublicDataset(**data.model_dump())
    db.add(item)
    await db.commit()
    await db.refresh(item)
    logger.info(f"新增公开数据集: {item.name}")
    return PublicDatasetResponse.model_validate(item)


@router.put("/{dataset_id}", summary="编辑公开数据集")
async def update_dataset(
    dataset_id: UUID,
    data: PublicDatasetUpdate,
    _admin=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PublicDataset).where(PublicDataset.id == dataset_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="数据集不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(item, key, value)

    await db.commit()
    await db.refresh(item)
    logger.info(f"更新公开数据集: {item.name}")
    return PublicDatasetResponse.model_validate(item)


@router.delete("/{dataset_id}", summary="删除公开数据集")
async def delete_dataset(
    dataset_id: UUID,
    _admin=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(PublicDataset).where(PublicDataset.id == dataset_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="数据集不存在")

    await db.delete(item)
    await db.commit()
    logger.info(f"删除公开数据集: {item.name}")
    return {"message": "删除成功"}
