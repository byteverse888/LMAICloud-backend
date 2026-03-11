"""
计费API测试
测试余额查询、充值、支付、账单等功能
"""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Recharge, Order


class TestBilling:
    """计费相关API测试"""
    
    async def test_get_balance(self, client: AsyncClient, test_user, auth_headers):
        """测试获取余额"""
        response = await client.get("/api/v1/billing/balance", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "balance" in data
        assert data["balance"] == 1000.0
    
    async def test_get_recharges(self, client: AsyncClient, test_user, auth_headers):
        """测试获取充值记录"""
        response = await client.get("/api/v1/billing/recharges", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "list" in data
        assert "total" in data
    
    async def test_get_orders(self, client: AsyncClient, test_user, auth_headers):
        """测试获取订单列表"""
        response = await client.get("/api/v1/billing/orders", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "list" in data
    
    async def test_create_payment_wechat(self, client: AsyncClient, test_user, auth_headers):
        """测试创建微信支付订单"""
        response = await client.post(
            "/api/v1/billing/pay",
            headers=auth_headers,
            json={
                "amount": 100,
                "payment_method": "wechat"
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "order_id" in data
        assert "qr_code_url" in data
        assert data["amount"] == 100
    
    async def test_create_payment_alipay(self, client: AsyncClient, test_user, auth_headers):
        """测试创建支付宝支付订单"""
        response = await client.post(
            "/api/v1/billing/pay",
            headers=auth_headers,
            json={
                "amount": 200,
                "payment_method": "alipay"
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "order_id" in data
        assert data["payment_method"] == "alipay"
    
    async def test_create_payment_invalid_amount(self, client: AsyncClient, auth_headers):
        """测试无效金额"""
        response = await client.post(
            "/api/v1/billing/pay",
            headers=auth_headers,
            json={
                "amount": 0,
                "payment_method": "wechat"
            }
        )
        assert response.status_code == 400
    
    async def test_check_payment_status(
        self, client: AsyncClient, test_session: AsyncSession, 
        test_user, auth_headers
    ):
        """测试查询支付状态"""
        # 先创建充值记录
        recharge = Recharge(
            id="test-recharge-001",
            user_id=test_user.id,
            amount=100.0,
            payment_method="wechat",
            status="pending",
            order_no="TEST202401010001",
        )
        test_session.add(recharge)
        await test_session.commit()
        
        response = await client.get(
            f"/api/v1/billing/pay/{recharge.order_no}/status",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "pending"
    
    async def test_mock_payment_success(
        self, client: AsyncClient, test_session: AsyncSession,
        test_user, auth_headers
    ):
        """测试模拟支付成功"""
        # 先创建充值记录
        recharge = Recharge(
            id="test-recharge-002",
            user_id=test_user.id,
            amount=100.0,
            payment_method="wechat",
            status="pending",
            order_no="TEST202401010002",
        )
        test_session.add(recharge)
        await test_session.commit()
        
        response = await client.post(
            f"/api/v1/billing/pay/mock/{recharge.order_no}",
            headers=auth_headers
        )
        assert response.status_code == 200
        
        # 验证余额已增加
        balance_response = await client.get("/api/v1/billing/balance", headers=auth_headers)
        new_balance = balance_response.json()["balance"]
        assert new_balance == 1100.0  # 原1000 + 充值100
    
    async def test_get_billing_details(self, client: AsyncClient, auth_headers):
        """测试获取消费明细"""
        response = await client.get(
            "/api/v1/billing/details?page=1&size=10",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert "list" in data
    
    async def test_get_billing_statements(self, client: AsyncClient, auth_headers):
        """测试获取账单汇总"""
        response = await client.get(
            "/api/v1/billing/statements?period=month",
            headers=auth_headers
        )
        assert response.status_code == 200
    
    async def test_unauthorized_access(self, client: AsyncClient):
        """测试未授权访问"""
        response = await client.get("/api/v1/billing/balance")
        assert response.status_code == 401


class TestMarket:
    """市场相关API测试"""
    
    async def test_list_machines(self, client: AsyncClient, test_session: AsyncSession):
        """测试获取机器列表"""
        # 创建测试节点
        from app.models import Node
        node = Node(
            id="market-node-001",
            name="市场节点",
            region="beijing-b",
            status="online",
            gpu_model="RTX 4090",
            gpu_total=8,
            gpu_available=4,
            gpu_memory=24,
            cpu_cores=64,
            memory=256,
            disk=2000,
            hourly_price=2.5,
        )
        test_session.add(node)
        await test_session.commit()
        
        response = await client.get("/api/v1/market/machines")
        assert response.status_code == 200
        data = response.json()
        assert "list" in data
    
    async def test_list_machines_with_filter(self, client: AsyncClient):
        """测试按条件筛选机器"""
        response = await client.get(
            "/api/v1/market/machines?region=beijing-b&gpu_model=RTX 4090"
        )
        assert response.status_code == 200
    
    async def test_list_regions(self, client: AsyncClient):
        """测试获取区域列表"""
        response = await client.get("/api/v1/market/regions")
        assert response.status_code == 200
        data = response.json()
        assert "regions" in data
    
    async def test_list_gpu_models(self, client: AsyncClient):
        """测试获取GPU型号列表"""
        response = await client.get("/api/v1/market/gpu-models")
        assert response.status_code == 200
        data = response.json()
        assert "gpu_models" in data
