"""管理端市场产品 CRUD API"""
import logging
from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.database import get_db
from app.utils.auth import get_current_admin_user
from app.models import MarketProduct, MarketCategory
from app.schemas import MarketProductCreate, MarketProductUpdate, MarketProductResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/products")
async def list_products(
    page: int = 1,
    size: int = 50,
    category: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取所有市场产品（含未上架）"""
    offset = (page - 1) * size
    conditions = []
    if category:
        conditions.append(MarketProduct.category == category)

    count_q = select(func.count(MarketProduct.id))
    if conditions:
        count_q = count_q.where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    q = select(MarketProduct)
    if conditions:
        q = q.where(*conditions)
    q = q.order_by(MarketProduct.sort_order, desc(MarketProduct.created_at)).offset(offset).limit(size)
    result = await db.execute(q)
    products = result.scalars().all()

    return {
        "list": [MarketProductResponse.model_validate(p).model_dump() for p in products],
        "total": total,
        "page": page,
        "size": size,
    }


@router.post("/products")
async def create_product(
    data: MarketProductCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """创建市场产品"""
    product = MarketProduct(
        category=data.category,
        name=data.name,
        description=data.description,
        icon=data.icon,
        specs=data.specs,
        price=data.price,
        price_unit=data.price_unit,
        tags=data.tags,
        sort_order=data.sort_order,
        is_active=data.is_active,
    )
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return MarketProductResponse.model_validate(product).model_dump()


@router.put("/products/{product_id}")
async def update_product(
    product_id: UUID,
    data: MarketProductUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """更新市场产品"""
    result = await db.execute(select(MarketProduct).where(MarketProduct.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="产品不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(product, key, value)

    await db.commit()
    await db.refresh(product)
    return MarketProductResponse.model_validate(product).model_dump()


@router.delete("/products/{product_id}")
async def delete_product(
    product_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """删除市场产品"""
    result = await db.execute(select(MarketProduct).where(MarketProduct.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="产品不存在")

    await db.delete(product)
    await db.commit()
    return {"message": "产品已删除"}
