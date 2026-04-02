"""
公开数据集 API（用户端，无需登录）
"""
from typing import Optional
from fastapi import APIRouter, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_

from app.database import get_db
from app.models import PublicDataset
from app.schemas import PaginatedResponse, PublicDatasetResponse

router = APIRouter()


@router.get("", response_model=PaginatedResponse, summary="获取公开数据集列表")
async def list_public_datasets(
    page: int = 1,
    size: int = 20,
    category: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """公开数据集列表，支持分类筛选和关键字搜索"""
    query = select(PublicDataset).where(PublicDataset.is_active == True)

    if category and category != "all":
        query = query.where(PublicDataset.category == category)

    if search:
        keyword = f"%{search}%"
        query = query.where(
            or_(
                PublicDataset.name.ilike(keyword),
                PublicDataset.description.ilike(keyword),
            )
        )

    # 总数
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # 分页
    query = query.order_by(PublicDataset.sort_order, PublicDataset.downloads.desc())
    query = query.offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    items = result.scalars().all()

    return PaginatedResponse(
        list=[PublicDatasetResponse.model_validate(i) for i in items],
        total=total,
        page=page,
        size=size,
    )
