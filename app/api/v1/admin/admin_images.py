"""应用镜像管理 API (管理端)"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import Optional
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel
import json

from app.database import get_db
from app.models import AppImage, AppImageStatus
from app.utils.auth import get_current_admin_user

router = APIRouter()


class AppImageCreate(BaseModel):
    """创建应用镜像"""
    name: str
    tag: str
    category: str = "base"
    description: Optional[str] = None
    icon: Optional[str] = None
    image_url: Optional[str] = None
    size_gb: float = 0
    config: Optional[dict] = None
    is_public: bool = True
    sort_order: int = 0


class AppImageUpdate(BaseModel):
    """更新应用镜像"""
    name: Optional[str] = None
    tag: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    image_url: Optional[str] = None
    size_gb: Optional[float] = None
    config: Optional[dict] = None
    status: Optional[str] = None
    is_public: Optional[bool] = None
    sort_order: Optional[int] = None


@router.get("/")
async def list_app_images(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    category: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取应用镜像列表"""
    # 统计总数
    count_query = select(func.count(AppImage.id))
    if category:
        count_query = count_query.where(AppImage.category == category)
    if status:
        count_query = count_query.where(AppImage.status == status)
    if search:
        count_query = count_query.where(AppImage.name.ilike(f"%{search}%"))
    
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    
    # 分页查询
    query = select(AppImage)
    if category:
        query = query.where(AppImage.category == category)
    if status:
        query = query.where(AppImage.status == status)
    if search:
        query = query.where(AppImage.name.ilike(f"%{search}%"))
    
    skip = (page - 1) * size
    query = query.order_by(AppImage.sort_order.asc(), AppImage.created_at.desc()).offset(skip).limit(size)
    result = await db.execute(query)
    images = result.scalars().all()
    
    image_list = []
    for img in images:
        config_data = None
        if img.config:
            try:
                config_data = json.loads(img.config)
            except:
                config_data = None
        
        image_list.append({
            "id": str(img.id),
            "name": img.name,
            "tag": img.tag,
            "category": img.category,
            "description": img.description,
            "icon": img.icon,
            "image_url": img.image_url,
            "size_gb": img.size_gb,
            "config": config_data,
            "status": img.status.value if hasattr(img.status, 'value') else str(img.status),
            "is_public": img.is_public,
            "sort_order": img.sort_order,
            "created_at": img.created_at.strftime("%Y-%m-%d %H:%M") if img.created_at else "",
        })
    
    return {"list": image_list, "total": total}


@router.get("/{image_id}")
async def get_app_image(
    image_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """获取应用镜像详情"""
    result = await db.execute(select(AppImage).where(AppImage.id == image_id))
    img = result.scalar_one_or_none()
    if not img:
        raise HTTPException(status_code=404, detail="镜像不存在")
    
    config_data = None
    if img.config:
        try:
            config_data = json.loads(img.config)
        except:
            config_data = None
    
    return {
        "id": str(img.id),
        "name": img.name,
        "tag": img.tag,
        "category": img.category,
        "description": img.description,
        "icon": img.icon,
        "image_url": img.image_url,
        "size_gb": img.size_gb,
        "config": config_data,
        "status": img.status.value if hasattr(img.status, 'value') else str(img.status),
        "is_public": img.is_public,
        "sort_order": img.sort_order,
        "created_at": img.created_at.strftime("%Y-%m-%d %H:%M:%S") if img.created_at else "",
        "updated_at": img.updated_at.strftime("%Y-%m-%d %H:%M:%S") if img.updated_at else "",
    }


@router.post("/")
async def create_app_image(
    data: AppImageCreate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """创建应用镜像"""
    config_str = None
    if data.config:
        config_str = json.dumps(data.config, ensure_ascii=False)
    
    image = AppImage(
        name=data.name,
        tag=data.tag,
        category=data.category,
        description=data.description,
        icon=data.icon,
        image_url=data.image_url,
        size_gb=data.size_gb,
        config=config_str,
        is_public=data.is_public,
        sort_order=data.sort_order,
        status=AppImageStatus.ACTIVE,
    )
    db.add(image)
    await db.commit()
    await db.refresh(image)
    
    return {"message": "镜像创建成功", "id": str(image.id)}


@router.put("/{image_id}")
async def update_app_image(
    image_id: UUID,
    data: AppImageUpdate,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """更新应用镜像"""
    result = await db.execute(select(AppImage).where(AppImage.id == image_id))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="镜像不存在")
    
    update_data = data.model_dump(exclude_unset=True)
    
    # 处理config字段
    if 'config' in update_data and update_data['config'] is not None:
        update_data['config'] = json.dumps(update_data['config'], ensure_ascii=False)
    
    # 处理status字段
    if 'status' in update_data:
        update_data['status'] = AppImageStatus(update_data['status'])
    
    for key, value in update_data.items():
        setattr(image, key, value)
    
    image.updated_at = datetime.utcnow()
    await db.commit()
    
    return {"message": "镜像更新成功"}


@router.delete("/{image_id}")
async def delete_app_image(
    image_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """删除应用镜像"""
    result = await db.execute(select(AppImage).where(AppImage.id == image_id))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="镜像不存在")
    
    await db.delete(image)
    await db.commit()
    
    return {"message": "镜像已删除"}


@router.put("/{image_id}/status")
async def update_app_image_status(
    image_id: UUID,
    status: str = Query(..., regex="^(active|inactive)$"),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_admin_user),
):
    """更新应用镜像状态"""
    result = await db.execute(select(AppImage).where(AppImage.id == image_id))
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="镜像不存在")
    
    image.status = AppImageStatus(status)
    image.updated_at = datetime.utcnow()
    await db.commit()
    
    return {"message": f"镜像状态已更新为 {status}"}
