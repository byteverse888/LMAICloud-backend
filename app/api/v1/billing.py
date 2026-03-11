"""
计费与支付 API

提供订单查询、充值、支付回调等功能
"""
import uuid
import hashlib
import hmac
import time
import json
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, Order, Recharge, OrderType, OrderStatus, RechargeStatus, PaymentMethod
from app.schemas import OrderResponse, RechargeCreate, RechargeResponse, PaginatedResponse, PaymentCreate, PaymentResponse
from app.utils.auth import get_current_user
from app.services.ws_manager import broadcast_billing_update
from app.config import settings

router = APIRouter()


# ========== 模拟支付配置 ==========
# 生产环境应从环境变量或配置文件读取
WECHAT_APP_ID = "wx_mock_appid"
WECHAT_MCH_ID = "mock_mch_id"
WECHAT_API_KEY = "mock_api_key_32char_here_12345678"

ALIPAY_APP_ID = "mock_alipay_appid"
ALIPAY_PRIVATE_KEY = "mock_private_key"


def generate_order_id() -> str:
    """生成订单号"""
    timestamp = int(time.time() * 1000)
    random_suffix = uuid.uuid4().hex[:6].upper()
    return f"{timestamp}{random_suffix}"


def generate_mock_qr_url(order_id: str, amount: float, method: str) -> str:
    """生成模拟支付二维码URL"""
    # 实际对接时调用微信/支付宝SDK生成真实二维码
    return f"https://pay.lmaicloud.com/qr/{method}/{order_id}?amount={amount}"


@router.get("/orders", response_model=PaginatedResponse, summary="获取订单列表")
async def list_orders(
    page: int = 1,
    size: int = 20,
    type: Optional[str] = None,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    获取用户订单列表
    
    - **page**: 页码
    - **size**: 每页数量
    - **type**: 订单类型 (create/renew/release/refund)
    - **status**: 订单状态 (pending/paid/completed/cancelled)
    """
    query = select(Order).where(Order.user_id == current_user.id)
    
    if type:
        query = query.where(Order.type == type)
    if status:
        query = query.where(Order.status == status)
    
    query = query.order_by(Order.created_at.desc())
    
    count_result = await db.execute(query)
    total = len(count_result.scalars().all())
    
    query = query.offset((page - 1) * size).limit(size)
    result = await db.execute(query)
    orders = result.scalars().all()
    
    return PaginatedResponse(
        list=[OrderResponse.model_validate(o) for o in orders],
        total=total,
        page=page,
        size=size
    )


@router.get("/balance", summary="获取账户余额")
async def get_balance(
    current_user: User = Depends(get_current_user)
):
    """
    获取当前用户的账户余额信息
    
    **返回字段:**
    - **balance**: 总余额
    - **frozen_balance**: 冻结余额(待结算)
    - **available**: 可用余额
    """
    return {
        "balance": current_user.balance,
        "frozen_balance": current_user.frozen_balance,
        "available": current_user.balance - current_user.frozen_balance
    }


@router.post("/recharge", response_model=RechargeResponse)
async def create_recharge(
    recharge_data: RechargeCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """创建充值记录"""
    recharge = Recharge(
        user_id=current_user.id,
        amount=recharge_data.amount,
        payment_method=recharge_data.payment_method
    )
    
    db.add(recharge)
    await db.commit()
    await db.refresh(recharge)
    
    return recharge


@router.post("/pay")
async def create_payment(
    payment_data: PaymentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    创建支付订单
    
    返回支付二维码URL或支付跳转URL
    """
    if payment_data.amount < 1:
        raise HTTPException(status_code=400, detail="Minimum recharge amount is ¥1")
    
    if payment_data.amount > 50000:
        raise HTTPException(status_code=400, detail="Maximum recharge amount is ¥50000")
    
    # 生成订单
    order_id = generate_order_id()
    
    # 创建充值记录
    recharge = Recharge(
        user_id=current_user.id,
        amount=payment_data.amount,
        payment_method=payment_data.payment_method,
        transaction_id=order_id,
        status=RechargeStatus.PENDING
    )
    db.add(recharge)
    await db.commit()
    await db.refresh(recharge)
    
    # 生成支付链接
    if payment_data.payment_method == "wechat":
        qr_url = generate_mock_qr_url(order_id, payment_data.amount, "wechat")
        pay_url = f"weixin://wxpay/bizpayurl?pr={order_id}"
    elif payment_data.payment_method == "alipay":
        qr_url = generate_mock_qr_url(order_id, payment_data.amount, "alipay")
        pay_url = f"https://openapi.alipay.com/gateway.do?order={order_id}"
    else:
        raise HTTPException(status_code=400, detail="Unsupported payment method")
    
    return {
        "order_id": order_id,
        "recharge_id": str(recharge.id),
        "amount": payment_data.amount,
        "payment_method": payment_data.payment_method,
        "qr_code_url": qr_url,
        "pay_url": pay_url,
        "expire_time": (datetime.utcnow() + timedelta(minutes=30)).isoformat(),
        "status": "pending"
    }


@router.get("/pay/{order_id}/status")
async def check_payment_status(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """查询支付状态"""
    result = await db.execute(
        select(Recharge).where(
            Recharge.transaction_id == order_id,
            Recharge.user_id == current_user.id
        )
    )
    recharge = result.scalar_one_or_none()
    
    if not recharge:
        raise HTTPException(status_code=404, detail="Order not found")
    
    return {
        "order_id": order_id,
        "amount": recharge.amount,
        "status": recharge.status,
        "created_at": recharge.created_at.isoformat()
    }


@router.post("/pay/callback/wechat")
async def wechat_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    微信支付回调
    
    接收微信支付结果通知
    """
    # 解析回调数据
    body = await request.body()
    
    # TODO: 验证签名
    # 实际对接时需要验证微信签名
    
    try:
        # 模拟解析XML数据
        # 实际对接时使用xml.etree解析
        data = {
            "return_code": "SUCCESS",
            "result_code": "SUCCESS",
            "out_trade_no": "mock_order_id",
            "transaction_id": "wx_mock_transaction_id"
        }
        
        if data.get("return_code") == "SUCCESS" and data.get("result_code") == "SUCCESS":
            order_id = data.get("out_trade_no")
            transaction_id = data.get("transaction_id")
            
            # 更新充值状态
            result = await db.execute(
                select(Recharge).where(Recharge.transaction_id == order_id)
            )
            recharge = result.scalar_one_or_none()
            
            if recharge and recharge.status == RechargeStatus.PENDING:
                recharge.status = RechargeStatus.SUCCESS
                
                # 更新用户余额
                result = await db.execute(
                    select(User).where(User.id == recharge.user_id)
                )
                user = result.scalar_one_or_none()
                if user:
                    user.balance += recharge.amount
                    
                    # 异步推送余额更新
                    background_tasks.add_task(
                        broadcast_billing_update,
                        str(user.id),
                        user.balance,
                        "recharge_success"
                    )
                
                await db.commit()
        
        return {"code": "SUCCESS", "message": "OK"}
    
    except Exception as e:
        return {"code": "FAIL", "message": str(e)}


@router.post("/pay/callback/alipay")
async def alipay_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    支付宝回调
    
    接收支付宝支付结果通知
    """
    form_data = await request.form()
    
    # TODO: 验证签名
    # 实际对接时需要验证支付宝签名
    
    try:
        trade_status = form_data.get("trade_status")
        out_trade_no = form_data.get("out_trade_no")
        trade_no = form_data.get("trade_no")
        
        if trade_status in ["TRADE_SUCCESS", "TRADE_FINISHED"]:
            result = await db.execute(
                select(Recharge).where(Recharge.transaction_id == out_trade_no)
            )
            recharge = result.scalar_one_or_none()
            
            if recharge and recharge.status == RechargeStatus.PENDING:
                recharge.status = RechargeStatus.SUCCESS
                
                result = await db.execute(
                    select(User).where(User.id == recharge.user_id)
                )
                user = result.scalar_one_or_none()
                if user:
                    user.balance += recharge.amount
                    
                    background_tasks.add_task(
                        broadcast_billing_update,
                        str(user.id),
                        user.balance,
                        "recharge_success"
                    )
                
                await db.commit()
        
        return "success"
    
    except Exception as e:
        return "fail"


@router.post("/pay/mock/{order_id}")
async def mock_payment_success(
    order_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    模拟支付成功 (仅用于开发测试)
    
    调用此接口模拟支付完成
    """
    result = await db.execute(
        select(Recharge).where(
            Recharge.transaction_id == order_id,
            Recharge.user_id == current_user.id
        )
    )
    recharge = result.scalar_one_or_none()
    
    if not recharge:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if recharge.status != RechargeStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Order already {recharge.status}")
    
    # 更新状态
    recharge.status = RechargeStatus.SUCCESS
    
    # 更新余额
    current_user.balance += recharge.amount
    
    await db.commit()
    
    # 推送更新
    background_tasks.add_task(
        broadcast_billing_update,
        str(current_user.id),
        current_user.balance,
        "recharge_success"
    )
    
    return {
        "message": "Payment successful (mock)",
        "order_id": order_id,
        "amount": recharge.amount,
        "new_balance": current_user.balance
    }


@router.get("/transactions")
async def list_transactions(
    page: int = 1,
    size: int = 20,
    type: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取交易流水"""
    # 合并订单和充值记录
    transactions = []
    
    # 获取订单
    orders_result = await db.execute(
        select(Order).where(Order.user_id == current_user.id).order_by(Order.created_at.desc())
    )
    for order in orders_result.scalars().all():
        transactions.append({
            "id": str(order.id),
            "type": order.type,
            "amount": -order.amount,  # 支出为负
            "status": order.status,
            "created_at": order.created_at.isoformat(),
            "description": f"{order.type}订单"
        })
    
    # 获取充值
    recharges_result = await db.execute(
        select(Recharge).where(
            Recharge.user_id == current_user.id,
            Recharge.status == RechargeStatus.SUCCESS
        ).order_by(Recharge.created_at.desc())
    )
    for recharge in recharges_result.scalars().all():
        transactions.append({
            "id": str(recharge.id),
            "type": "recharge",
            "amount": recharge.amount,  # 收入为正
            "status": "success",
            "created_at": recharge.created_at.isoformat(),
            "description": f"{recharge.payment_method}充值"
        })
    
    # 按时间排序
    transactions.sort(key=lambda x: x["created_at"], reverse=True)
    
    # 分页
    total = len(transactions)
    start = (page - 1) * size
    end = start + size
    
    return {
        "list": transactions[start:end],
        "total": total,
        "page": page,
        "size": size
    }
