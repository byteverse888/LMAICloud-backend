"""用户端工单API"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from uuid import UUID
from typing import Optional
from datetime import datetime

from app.database import get_db
from app.models import Ticket, TicketStatus, TicketCategory, TicketPriority
from app.schemas import TicketCreate, TicketUpdate, TicketResponse, PaginatedResponse
from app.utils.auth import get_current_user
from app.logging_config import get_logger

router = APIRouter()
logger = get_logger("lmaicloud.tickets")


@router.post("", response_model=TicketResponse)
async def create_ticket(
    ticket_data: TicketCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """创建工单"""
    logger.info(f"用户 {current_user.id} 创建工单: {ticket_data.title}")
    
    try:
        # 验证分类和优先级
        try:
            category = TicketCategory(ticket_data.category)
        except ValueError:
            category = TicketCategory.OTHER
        
        try:
            priority = TicketPriority(ticket_data.priority)
        except ValueError:
            priority = TicketPriority.MEDIUM
        
        ticket = Ticket(
            user_id=current_user.id,
            title=ticket_data.title,
            content=ticket_data.content,
            category=category,
            priority=priority,
            status=TicketStatus.OPEN,
        )
        db.add(ticket)
        await db.commit()
        await db.refresh(ticket)
        
        logger.info(f"工单创建成功: {ticket.id}")
        return TicketResponse(
            id=ticket.id,
            user_id=ticket.user_id,
            title=ticket.title,
            content=ticket.content,
            category=ticket.category.value,
            priority=ticket.priority.value,
            status=ticket.status.value,
            handler_id=ticket.handler_id,
            reply=ticket.reply,
            replied_at=ticket.replied_at,
            resolved_at=ticket.resolved_at,
            closed_at=ticket.closed_at,
            created_at=ticket.created_at,
            updated_at=ticket.updated_at,
            user_email=current_user.email,
            user_nickname=current_user.nickname,
        )
    except Exception as e:
        logger.error(f"创建工单失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="创建工单失败")


@router.get("", response_model=PaginatedResponse)
async def list_my_tickets(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """获取我的工单列表"""
    query = select(Ticket).where(Ticket.user_id == current_user.id)
    
    if status:
        try:
            ticket_status = TicketStatus(status)
            query = query.where(Ticket.status == ticket_status)
        except ValueError:
            pass
    
    # 计算总数
    count_query = select(func.count(Ticket.id)).where(Ticket.user_id == current_user.id)
    if status:
        try:
            ticket_status = TicketStatus(status)
            count_query = count_query.where(Ticket.status == ticket_status)
        except ValueError:
            pass
    
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    
    # 分页查询
    query = query.order_by(Ticket.created_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    tickets = result.scalars().all()
    
    ticket_list = [
        TicketResponse(
            id=t.id,
            user_id=t.user_id,
            title=t.title,
            content=t.content,
            category=t.category.value,
            priority=t.priority.value,
            status=t.status.value,
            handler_id=t.handler_id,
            reply=t.reply,
            replied_at=t.replied_at,
            resolved_at=t.resolved_at,
            closed_at=t.closed_at,
            created_at=t.created_at,
            updated_at=t.updated_at,
        ).model_dump()
        for t in tickets
    ]
    
    return PaginatedResponse(list=ticket_list, total=total, page=page, size=size)


@router.get("/{ticket_id}", response_model=TicketResponse)
async def get_ticket(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """获取工单详情"""
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == current_user.id)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    return TicketResponse(
        id=ticket.id,
        user_id=ticket.user_id,
        title=ticket.title,
        content=ticket.content,
        category=ticket.category.value,
        priority=ticket.priority.value,
        status=ticket.status.value,
        handler_id=ticket.handler_id,
        reply=ticket.reply,
        replied_at=ticket.replied_at,
        resolved_at=ticket.resolved_at,
        closed_at=ticket.closed_at,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


@router.put("/{ticket_id}", response_model=TicketResponse)
async def update_ticket(
    ticket_id: UUID,
    ticket_data: TicketUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """更新工单（仅限待处理状态）"""
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == current_user.id)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    if ticket.status != TicketStatus.OPEN:
        raise HTTPException(status_code=400, detail="只能修改待处理的工单")
    
    if ticket_data.title:
        ticket.title = ticket_data.title
    if ticket_data.content:
        ticket.content = ticket_data.content
    if ticket_data.category:
        try:
            ticket.category = TicketCategory(ticket_data.category)
        except ValueError:
            pass
    if ticket_data.priority:
        try:
            ticket.priority = TicketPriority(ticket_data.priority)
        except ValueError:
            pass
    
    await db.commit()
    await db.refresh(ticket)
    
    return TicketResponse(
        id=ticket.id,
        user_id=ticket.user_id,
        title=ticket.title,
        content=ticket.content,
        category=ticket.category.value,
        priority=ticket.priority.value,
        status=ticket.status.value,
        handler_id=ticket.handler_id,
        reply=ticket.reply,
        replied_at=ticket.replied_at,
        resolved_at=ticket.resolved_at,
        closed_at=ticket.closed_at,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


@router.post("/{ticket_id}/close")
async def close_ticket(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """用户关闭工单"""
    result = await db.execute(
        select(Ticket).where(Ticket.id == ticket_id, Ticket.user_id == current_user.id)
    )
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=400, detail="工单已关闭")
    
    ticket.status = TicketStatus.CLOSED
    ticket.closed_at = datetime.utcnow()
    await db.commit()
    
    logger.info(f"用户 {current_user.id} 关闭工单 {ticket_id}")
    return {"message": "工单已关闭"}
