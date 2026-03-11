from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import AppImage, AppImageStatus

router = APIRouter()


@router.get("")
async def list_images(
    page: int = 1,
    size: int = 20,
    category: str = None,
    db: AsyncSession = Depends(get_db)
):
    """获取公开的镜像列表（从app_images表）"""
    query = select(AppImage).where(
        AppImage.is_public == True,
        AppImage.status == AppImageStatus.ACTIVE
    )
    
    if category:
        query = query.where(AppImage.category == category)
    
    # 按排序和创建时间排序
    query = query.order_by(AppImage.sort_order.asc(), AppImage.created_at.desc())
    
    # 统计总数
    count_result = await db.execute(query)
    total = len(count_result.scalars().all())
    
    # 分页
    query = query.offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    images = result.scalars().all()
    
    return {
        "list": [
            {
                "id": str(img.id),
                "name": img.name,
                "tag": img.tag,
                "category": img.category,
                "description": img.description,
                "icon": img.icon,
                "image_url": img.image_url,
                "size_gb": img.size_gb or 0,
                "type": img.category,  # 兼容前端
                "is_public": img.is_public,
                "created_at": img.created_at.isoformat() if img.created_at else "",
            }
            for img in images
        ],
        "total": total,
        "page": page,
        "size": size
    }


@router.get("/{image_id}")
async def get_image(image_id: str, db: AsyncSession = Depends(get_db)):
    """获取单个镜像详情"""
    from uuid import UUID
    result = await db.execute(
        select(AppImage).where(AppImage.id == UUID(image_id))
    )
    image = result.scalar_one_or_none()
    
    if not image:
        return {"detail": "Image not found"}
    
    return {
        "id": str(image.id),
        "name": image.name,
        "tag": image.tag,
        "category": image.category,
        "description": image.description,
        "icon": image.icon,
        "image_url": image.image_url,
        "size_gb": image.size_gb or 0,
        "is_public": image.is_public,
        "created_at": image.created_at.isoformat() if image.created_at else "",
    }
