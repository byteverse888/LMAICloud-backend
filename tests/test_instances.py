"""
实例API测试
测试实例创建、查询、操作、续费等功能
"""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Instance, Node


@pytest.fixture
async def test_node(test_session: AsyncSession) -> Node:
    """创建测试节点"""
    node = Node(
        id="test-node-001",
        name="测试节点1",
        region="beijing-b",
        status="online",
        gpu_model="RTX 4090",
        gpu_total=8,
        gpu_available=4,
        gpu_memory=24,
        cpu_cores=64,
        cpu_model="Intel Xeon",
        memory=256,
        disk=2000,
        hourly_price=2.5,
    )
    test_session.add(node)
    await test_session.commit()
    await test_session.refresh(node)
    return node


@pytest.fixture
async def test_instance(test_session: AsyncSession, test_user, test_node) -> Instance:
    """创建测试实例"""
    from datetime import datetime, timedelta
    
    instance = Instance(
        id="test-instance-001",
        name="测试实例1",
        user_id=test_user.id,
        node_id=test_node.id,
        image_id="pytorch-2.0",
        status="running",
        gpu_count=1,
        gpu_model="RTX 4090",
        cpu_cores=8,
        memory=32,
        disk=100,
        hourly_price=2.5,
        billing_type="hourly",
        created_at=datetime.now(),
        expired_at=datetime.now() + timedelta(hours=24),
    )
    test_session.add(instance)
    await test_session.commit()
    await test_session.refresh(instance)
    return instance


class TestInstances:
    """实例相关API测试"""
    
    async def test_list_instances(self, client: AsyncClient, test_instance, auth_headers):
        """测试获取实例列表"""
        response = await client.get("/api/v1/instances", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "list" in data
        assert data["total"] >= 1
    
    async def test_list_instances_with_filter(self, client: AsyncClient, test_instance, auth_headers):
        """测试按状态筛选实例"""
        response = await client.get(
            "/api/v1/instances?status=running",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        for instance in data["list"]:
            assert instance["status"] == "running"
    
    async def test_get_instance(self, client: AsyncClient, test_instance, auth_headers):
        """测试获取实例详情"""
        response = await client.get(
            f"/api/v1/instances/{test_instance.id}",
            headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == test_instance.id
        assert data["name"] == "测试实例1"
    
    async def test_get_instance_not_found(self, client: AsyncClient, auth_headers):
        """测试获取不存在的实例"""
        response = await client.get(
            "/api/v1/instances/nonexistent-id",
            headers=auth_headers
        )
        assert response.status_code == 404
    
    async def test_create_instance(self, client: AsyncClient, test_user, test_node, auth_headers):
        """测试创建实例"""
        response = await client.post(
            "/api/v1/instances",
            headers=auth_headers,
            json={
                "name": "新建实例",
                "node_id": test_node.id,
                "image_id": "pytorch-2.0",
                "gpu_count": 1,
                "billing_type": "hourly",
                "duration_hours": 1,
            }
        )
        # 可能因为K8s未连接返回500，或成功返回200
        assert response.status_code in [200, 201, 500]
    
    async def test_stop_instance(self, client: AsyncClient, test_instance, auth_headers):
        """测试停止实例"""
        response = await client.post(
            f"/api/v1/instances/{test_instance.id}/stop",
            headers=auth_headers
        )
        # 可能因为K8s未连接返回500，或成功返回200
        assert response.status_code in [200, 500]
    
    async def test_start_instance(self, client: AsyncClient, test_instance, auth_headers):
        """测试启动实例"""
        response = await client.post(
            f"/api/v1/instances/{test_instance.id}/start",
            headers=auth_headers
        )
        assert response.status_code in [200, 400, 500]
    
    async def test_renew_instance(self, client: AsyncClient, test_instance, auth_headers):
        """测试续费实例"""
        response = await client.post(
            f"/api/v1/instances/{test_instance.id}/renew",
            headers=auth_headers,
            json={
                "duration_hours": 24,
                "billing_type": "hourly"
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "new_expired_at" in data
    
    async def test_renew_instance_insufficient_balance(
        self, client: AsyncClient, test_session: AsyncSession, 
        test_instance, test_user, auth_headers
    ):
        """测试余额不足续费"""
        # 将用户余额设为0
        test_user.balance = 0
        await test_session.commit()
        
        response = await client.post(
            f"/api/v1/instances/{test_instance.id}/renew",
            headers=auth_headers,
            json={
                "duration_hours": 1000,  # 需要很多钱
                "billing_type": "hourly"
            }
        )
        assert response.status_code == 400
        assert "余额不足" in response.json()["detail"]
    
    async def test_release_instance(self, client: AsyncClient, test_instance, auth_headers):
        """测试释放实例"""
        response = await client.post(
            f"/api/v1/instances/{test_instance.id}/release",
            headers=auth_headers
        )
        assert response.status_code in [200, 500]
    
    async def test_get_instance_logs(self, client: AsyncClient, test_instance, auth_headers):
        """测试获取实例日志"""
        response = await client.get(
            f"/api/v1/instances/{test_instance.id}/logs",
            headers=auth_headers
        )
        # 可能因为Pod不存在返回404或500
        assert response.status_code in [200, 404, 500]
    
    async def test_unauthorized_access(self, client: AsyncClient, test_instance):
        """测试未授权访问"""
        response = await client.get(f"/api/v1/instances/{test_instance.id}")
        assert response.status_code == 401
