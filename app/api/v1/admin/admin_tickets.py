"""管理端工单API"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from uuid import UUID
from typing import Optional
from datetime import datetime

from app.database import get_db
from app.models import Ticket, TicketStatus, TicketCategory, TicketPriority, User
from app.schemas import TicketReply, TicketResponse, PaginatedResponse
from app.utils.auth import get_current_admin_user
from app.logging_config import get_logger

router = APIRouter()
logger = get_logger("lmaicloud.admin.tickets")


@router.get("", response_model=PaginatedResponse)
async def list_tickets(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=100),
    status: Optional[str] = None,
    category: Optional[str] = None,
    priority: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取所有工单列表（支持筛选和搜索）"""
    query = select(Ticket)
    count_query = select(func.count(Ticket.id))
    
    # 状态过滤
    if status:
        try:
            ticket_status = TicketStatus(status)
            query = query.where(Ticket.status == ticket_status)
            count_query = count_query.where(Ticket.status == ticket_status)
        except ValueError:
            pass
    
    # 分类过滤
    if category:
        try:
            ticket_category = TicketCategory(category)
            query = query.where(Ticket.category == ticket_category)
            count_query = count_query.where(Ticket.category == ticket_category)
        except ValueError:
            pass
    
    # 优先级过滤
    if priority:
        try:
            ticket_priority = TicketPriority(priority)
            query = query.where(Ticket.priority == ticket_priority)
            count_query = count_query.where(Ticket.priority == ticket_priority)
        except ValueError:
            pass
    
    # 搜索（标题和内容）
    if search:
        search_filter = or_(
            Ticket.title.ilike(f"%{search}%"),
            Ticket.content.ilike(f"%{search}%")
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    
    # 计算总数
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0
    
    # 分页查询
    query = query.order_by(Ticket.created_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    tickets = result.scalars().all()
    
    # 获取用户信息
    ticket_list = []
    for t in tickets:
        # 获取提交用户信息
        user_result = await db.execute(select(User).where(User.id == t.user_id))
        user = user_result.scalar_one_or_none()
        
        # 获取处理人信息
        handler_nickname = None
        if t.handler_id:
            handler_result = await db.execute(select(User).where(User.id == t.handler_id))
            handler = handler_result.scalar_one_or_none()
            if handler:
                handler_nickname = handler.nickname or handler.email
        
        ticket_list.append(
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
                user_email=user.email if user else None,
                user_nickname=user.nickname if user else None,
                handler_nickname=handler_nickname,
            ).model_dump()
        )
    
    return PaginatedResponse(list=ticket_list, total=total, page=page, size=size)


@router.get("/stats")
async def get_ticket_stats(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取工单统计"""
    # 各状态数量
    stats = {}
    for status in TicketStatus:
        result = await db.execute(
            select(func.count(Ticket.id)).where(Ticket.status == status)
        )
        stats[status.value] = result.scalar() or 0
    
    # 总数
    total_result = await db.execute(select(func.count(Ticket.id)))
    stats["total"] = total_result.scalar() or 0
    
    return stats


@router.get("/{ticket_id}", response_model=TicketResponse)
async def get_ticket(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """获取工单详情"""
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    # 获取用户信息
    user_result = await db.execute(select(User).where(User.id == ticket.user_id))
    user = user_result.scalar_one_or_none()
    
    # 获取处理人信息
    handler_nickname = None
    if ticket.handler_id:
        handler_result = await db.execute(select(User).where(User.id == ticket.handler_id))
        handler = handler_result.scalar_one_or_none()
        if handler:
            handler_nickname = handler.nickname or handler.email
    
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
        user_email=user.email if user else None,
        user_nickname=user.nickname if user else None,
        handler_nickname=handler_nickname,
    )


@router.post("/{ticket_id}/reply", response_model=TicketResponse)
async def reply_ticket(
    ticket_id: UUID,
    reply_data: TicketReply,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """回复工单"""
    logger.info(f"管理员 {current_user.id} 回复工单 {ticket_id}")
    
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=400, detail="工单已关闭，无法回复")
    
    ticket.reply = reply_data.reply
    ticket.replied_at = datetime.utcnow()
    ticket.handler_id = current_user.id
    
    # 更新状态
    if reply_data.status:
        try:
            new_status = TicketStatus(reply_data.status)
            ticket.status = new_status
            if new_status == TicketStatus.RESOLVED:
                ticket.resolved_at = datetime.utcnow()
            elif new_status == TicketStatus.CLOSED:
                ticket.closed_at = datetime.utcnow()
        except ValueError:
            pass
    else:
        # 默认设为处理中
        if ticket.status == TicketStatus.OPEN:
            ticket.status = TicketStatus.PROCESSING
    
    await db.commit()
    await db.refresh(ticket)
    
    logger.info(f"工单 {ticket_id} 回复成功，状态: {ticket.status.value}")
    
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
        handler_nickname=current_user.nickname or current_user.email,
    )


@router.post("/{ticket_id}/resolve")
async def resolve_ticket(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """标记工单为已解决"""
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=400, detail="工单已关闭")
    
    ticket.status = TicketStatus.RESOLVED
    ticket.resolved_at = datetime.utcnow()
    if not ticket.handler_id:
        ticket.handler_id = current_user.id
    
    await db.commit()
    
    logger.info(f"管理员 {current_user.id} 解决工单 {ticket_id}")
    return {"message": "工单已标记为已解决"}


@router.post("/{ticket_id}/close")
async def close_ticket(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """关闭工单"""
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=400, detail="工单已关闭")
    
    ticket.status = TicketStatus.CLOSED
    ticket.closed_at = datetime.utcnow()
    if not ticket.handler_id:
        ticket.handler_id = current_user.id
    
    await db.commit()
    
    logger.info(f"管理员 {current_user.id} 关闭工单 {ticket_id}")
    return {"message": "工单已关闭"}


@router.delete("/{ticket_id}")
async def delete_ticket(
    ticket_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_admin_user),
):
    """删除工单"""
    result = await db.execute(select(Ticket).where(Ticket.id == ticket_id))
    ticket = result.scalar_one_or_none()
    
    if not ticket:
        raise HTTPException(status_code=404, detail="工单不存在")
    
    await db.delete(ticket)
    await db.commit()
    
    logger.info(f"管理员 {current_user.id} 删除工单 {ticket_id}")
    return {"message": "工单已删除"}
