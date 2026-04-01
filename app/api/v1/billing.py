"""
计费与支付 API

提供订单查询、充值、支付回调、交易流水、账单统计、资源套餐等功能
"""
import uuid
import hashlib
import hmac
import time
import json
import base64
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, extract, case

from app.database import get_db
from app.models import User, Order, Recharge, OrderType, OrderStatus, RechargeStatus, PaymentMethod, ResourcePlan
from app.schemas import (
    OrderResponse, RechargeCreate, RechargeResponse, PaginatedResponse,
    PaymentCreate, PaymentResponse, ResourcePlanResponse,
)
from app.utils.auth import get_current_user
from app.services.ws_manager import broadcast_billing_update
from app.config import settings
from app.api.v1.points import add_points
from app.models import PointType

router = APIRouter()


def generate_order_id() -> str:
    """生成订单号"""
    timestamp = int(time.time() * 1000)
    random_suffix = uuid.uuid4().hex[:6].upper()
    return f"{timestamp}{random_suffix}"


def _is_wechat_configured() -> bool:
    """检查微信支付是否已配置"""
    return bool(settings.wechat_mch_id and settings.wechat_app_id)


async def _create_wechat_native_order(order_id: str, amount: float, description: str) -> dict:
    """
    调用微信支付 V3 Native 下单 API
    返回 {"code_url": "weixin://...", ...} 或抛出异常
    """
    import httpx
    import secrets
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    url = "https://api.mch.weixin.qq.com/v3/pay/transactions/native"
    nonce = secrets.token_hex(16)
    timestamp = str(int(time.time()))

    body = {
        "appid": settings.wechat_app_id,
        "mchid": settings.wechat_mch_id,
        "description": description,
        "out_trade_no": order_id,
        "notify_url": settings.wechat_notify_url,
        "amount": {
            "total": int(amount * 100),  # 分
            "currency": "CNY",
        },
    }
    body_str = json.dumps(body, ensure_ascii=False)

    # 构建签名串
    sign_str = f"POST\n/v3/pay/transactions/native\n{timestamp}\n{nonce}\n{body_str}\n"

    # 用商户私钥签名
    with open(settings.wechat_private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    signature = base64.b64encode(
        private_key.sign(sign_str.encode(), padding.PKCS1v15(), hashes.SHA256())
    ).decode()

    auth_header = (
        f'WECHATPAY2-SHA256-RSA2048 '
        f'mchid="{settings.wechat_mch_id}",'
        f'nonce_str="{nonce}",'
        f'signature="{signature}",'
        f'timestamp="{timestamp}",'
        f'serial_no="{settings.wechat_cert_serial_no}"'
    )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            content=body_str,
            headers={
                "Content-Type": "application/json",
                "Authorization": auth_header,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"微信支付下单失败: {resp.text}")
        return resp.json()


@router.get("/orders", response_model=PaginatedResponse, summary="获取订单列表")
async def list_orders(
    page: int = 1,
    size: int = 20,
    type: Optional[str] = None,
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取用户订单列表"""
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
    """获取当前用户的账户余额信息"""
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
    """创建支付订单，返回支付二维码URL"""
    if payment_data.amount < 1:
        raise HTTPException(status_code=400, detail="Minimum recharge amount is ¥1")
    if payment_data.amount > 50000:
        raise HTTPException(status_code=400, detail="Maximum recharge amount is ¥50000")

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

    qr_url = None
    pay_url = None

    if payment_data.payment_method == "wechat":
        if _is_wechat_configured():
            # 真实微信支付
            try:
                wx_result = await _create_wechat_native_order(
                    order_id, payment_data.amount, "LMAICloud 账户充值"
                )
                qr_url = wx_result.get("code_url", "")
                pay_url = qr_url
            except Exception as e:
                # 下单失败，回滚充值记录
                recharge.status = RechargeStatus.FAILED
                await db.commit()
                raise HTTPException(status_code=502, detail=f"微信支付下单失败: {str(e)}")
        else:
            # Mock 模式（开发/测试）
            qr_url = f"https://pay.lmaicloud.com/qr/wechat/{order_id}?amount={payment_data.amount}"
            pay_url = f"weixin://wxpay/bizpayurl?pr={order_id}"
    elif payment_data.payment_method == "alipay":
        # 支付宝暂保留 mock
        qr_url = f"https://pay.lmaicloud.com/qr/alipay/{order_id}?amount={payment_data.amount}"
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
    """微信支付 V3 回调"""
    body = await request.body()

    try:
        data = json.loads(body)
    except Exception:
        return {"code": "FAIL", "message": "invalid body"}

    # 解密通知体
    resource = data.get("resource", {})
    # 如果配置了 api_v3_key，使用 AEAD_AES_256_GCM 解密
    if settings.wechat_api_v3_key and resource.get("ciphertext"):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            key = settings.wechat_api_v3_key.encode("utf-8")
            nonce = resource["nonce"].encode("utf-8")
            associated_data = (resource.get("associated_data") or "").encode("utf-8")
            ciphertext = base64.b64decode(resource["ciphertext"])
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data)
            pay_data = json.loads(plaintext)
        except Exception as e:
            return {"code": "FAIL", "message": f"decrypt error: {e}"}
    else:
        # Mock 模式 / 未配置密钥 — 直接取 resource 或 data
        pay_data = resource if resource else data
        # 兼容 mock 回调
        if not pay_data.get("out_trade_no"):
            pay_data = {
                "trade_state": "SUCCESS",
                "out_trade_no": data.get("out_trade_no", ""),
                "transaction_id": data.get("transaction_id", ""),
            }

    if pay_data.get("trade_state") == "SUCCESS":
        order_id = pay_data.get("out_trade_no")
        result = await db.execute(
            select(Recharge).where(Recharge.transaction_id == order_id)
        )
        recharge = result.scalar_one_or_none()

        if recharge and recharge.status == RechargeStatus.PENDING:
            recharge.status = RechargeStatus.SUCCESS
            recharge.paid_at = datetime.utcnow()

            result = await db.execute(select(User).where(User.id == recharge.user_id))
            user = result.scalar_one_or_none()
            if user:
                user.balance += recharge.amount
                # 充值积分奖励: 充值X元赠送X积分(10%比例)
                reward_points = int(recharge.amount)
                if reward_points > 0:
                    await add_points(db, user.id, reward_points, PointType.RECHARGE_REWARD, f"充值 ¥{recharge.amount} 赠送积分")
                background_tasks.add_task(
                    broadcast_billing_update,
                    str(user.id), user.balance, "recharge_success"
                )
            await db.commit()

    return {"code": "SUCCESS", "message": "OK"}


@router.post("/pay/callback/alipay")
async def alipay_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """支付宝回调"""
    form_data = await request.form()
    try:
        trade_status = form_data.get("trade_status")
        out_trade_no = form_data.get("out_trade_no")
        if trade_status in ["TRADE_SUCCESS", "TRADE_FINISHED"]:
            result = await db.execute(
                select(Recharge).where(Recharge.transaction_id == out_trade_no)
            )
            recharge = result.scalar_one_or_none()
            if recharge and recharge.status == RechargeStatus.PENDING:
                recharge.status = RechargeStatus.SUCCESS
                recharge.paid_at = datetime.utcnow()
                result = await db.execute(select(User).where(User.id == recharge.user_id))
                user = result.scalar_one_or_none()
                if user:
                    user.balance += recharge.amount
                    # 充值积分奖励
                    reward_points = int(recharge.amount)
                    if reward_points > 0:
                        await add_points(db, user.id, reward_points, PointType.RECHARGE_REWARD, f"充值 ¥{recharge.amount} 赠送积分")
                    background_tasks.add_task(
                        broadcast_billing_update,
                        str(user.id), user.balance, "recharge_success"
                    )
                await db.commit()
        return "success"
    except Exception:
        return "fail"


@router.post("/pay/mock/{order_id}")
async def mock_payment_success(
    order_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """模拟支付成功 (仅用于开发测试)"""
    if settings.app_env not in ("development", "dev", "test"):
        raise HTTPException(status_code=403, detail="Mock payment only available in dev/test")

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

    recharge.status = RechargeStatus.SUCCESS
    recharge.paid_at = datetime.utcnow()
    current_user.balance += recharge.amount
    # 充值积分奖励
    reward_points = int(recharge.amount)
    if reward_points > 0:
        await add_points(db, current_user.id, reward_points, PointType.RECHARGE_REWARD, f"充值 ¥{recharge.amount} 赠送积分")
    await db.commit()

    background_tasks.add_task(
        broadcast_billing_update,
        str(current_user.id), current_user.balance, "recharge_success"
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
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取交易流水（合并消费+充值）"""
    transactions = []

    # 消费订单
    if type in (None, "consumption", "order"):
        orders_result = await db.execute(
            select(Order).where(Order.user_id == current_user.id).order_by(Order.created_at.desc())
        )
        for order in orders_result.scalars().all():
            transactions.append({
                "id": str(order.id),
                "type": "consumption",
                "amount": -abs(order.amount),
                "status": order.status,
                "created_at": order.created_at.isoformat(),
                "description": order.description or order.product_name or f"{order.type}订单",
                "product_name": order.product_name,
                "billing_cycle": order.billing_cycle,
            })

    # 充值记录
    if type in (None, "recharge"):
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
                "amount": recharge.amount,
                "status": "success",
                "created_at": recharge.created_at.isoformat(),
                "description": f"{recharge.payment_method}充值",
                "product_name": None,
                "billing_cycle": None,
            })

    # 日期过滤
    if start_date:
        transactions = [t for t in transactions if t["created_at"] >= start_date]
    if end_date:
        transactions = [t for t in transactions if t["created_at"] <= end_date]

    transactions.sort(key=lambda x: x["created_at"], reverse=True)
    total = len(transactions)
    start = (page - 1) * size
    end_idx = start + size

    return {
        "list": transactions[start:end_idx],
        "total": total,
        "page": page,
        "size": size
    }


@router.get("/statements")
async def list_statements(
    year: Optional[int] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """按月账单统计"""
    if not year:
        year = datetime.utcnow().year

    # 每月消费汇总
    consumption_result = await db.execute(
        select(
            extract("month", Order.created_at).label("month"),
            func.sum(func.abs(Order.amount)).label("total"),
            func.count(Order.id).label("count"),
        ).where(
            Order.user_id == current_user.id,
            extract("year", Order.created_at) == year,
        ).group_by(extract("month", Order.created_at))
    )
    consumption_map = {int(r.month): {"total": float(r.total or 0), "count": int(r.count)} for r in consumption_result}

    # 每月充值汇总
    recharge_result = await db.execute(
        select(
            extract("month", Recharge.created_at).label("month"),
            func.sum(Recharge.amount).label("total"),
            func.count(Recharge.id).label("count"),
        ).where(
            Recharge.user_id == current_user.id,
            Recharge.status == RechargeStatus.SUCCESS,
            extract("year", Recharge.created_at) == year,
        ).group_by(extract("month", Recharge.created_at))
    )
    recharge_map = {int(r.month): {"total": float(r.total or 0), "count": int(r.count)} for r in recharge_result}

    statements = []
    for m in range(1, 13):
        c = consumption_map.get(m, {"total": 0, "count": 0})
        r = recharge_map.get(m, {"total": 0, "count": 0})
        statements.append({
            "month": m,
            "year": year,
            "consumption": c["total"],
            "consumption_count": c["count"],
            "recharge": r["total"],
            "recharge_count": r["count"],
            "net": r["total"] - c["total"],
        })

    # 年度汇总
    total_consumption = sum(s["consumption"] for s in statements)
    total_recharge = sum(s["recharge"] for s in statements)

    return {
        "year": year,
        "statements": statements,
        "summary": {
            "total_consumption": total_consumption,
            "total_recharge": total_recharge,
            "balance": current_user.balance,
        },
    }


@router.get("/plans", summary="获取可用资源套餐列表")
async def list_plans(
    db: AsyncSession = Depends(get_db)
):
    """获取已启用的资源套餐列表"""
    result = await db.execute(
        select(ResourcePlan).where(ResourcePlan.is_active == True).order_by(ResourcePlan.sort_order)
    )
    plans = result.scalars().all()
    return [ResourcePlanResponse.model_validate(p) for p in plans]
