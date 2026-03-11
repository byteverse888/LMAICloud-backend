"""
认证API测试
测试注册、登录、刷新Token等功能
"""
import pytest
from httpx import AsyncClient


class TestAuth:
    """认证相关API测试"""
    
    async def test_register_success(self, client: AsyncClient):
        """测试用户注册成功"""
        response = await client.post("/api/v1/auth/register", json={
            "username": "newuser",
            "email": "newuser@example.com",
            "phone": "13800000001",
            "password": "NewPass123",
            "confirm_password": "NewPass123",
            "agreement": True
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"
    
    async def test_register_duplicate_username(self, client: AsyncClient, test_user):
        """测试重复用户名注册"""
        response = await client.post("/api/v1/auth/register", json={
            "username": "testuser",  # 已存在
            "email": "another@example.com",
            "phone": "13800000002",
            "password": "Pass123456",
            "confirm_password": "Pass123456",
            "agreement": True
        })
        assert response.status_code == 400
        assert "已被注册" in response.json()["detail"]
    
    async def test_register_password_mismatch(self, client: AsyncClient):
        """测试密码不匹配"""
        response = await client.post("/api/v1/auth/register", json={
            "username": "newuser2",
            "email": "newuser2@example.com",
            "phone": "13800000003",
            "password": "Pass123456",
            "confirm_password": "Different123",
            "agreement": True
        })
        assert response.status_code == 400
    
    async def test_login_success(self, client: AsyncClient, test_user):
        """测试登录成功"""
        response = await client.post("/api/v1/auth/login", json={
            "username": "testuser",
            "password": "Test123456"
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
    
    async def test_login_wrong_password(self, client: AsyncClient, test_user):
        """测试错误密码登录"""
        response = await client.post("/api/v1/auth/login", json={
            "username": "testuser",
            "password": "WrongPassword"
        })
        assert response.status_code == 401
    
    async def test_login_nonexistent_user(self, client: AsyncClient):
        """测试不存在的用户登录"""
        response = await client.post("/api/v1/auth/login", json={
            "username": "nonexistent",
            "password": "Password123"
        })
        assert response.status_code == 401
    
    async def test_refresh_token(self, client: AsyncClient, test_user, user_token):
        """测试刷新Token"""
        # 先登录获取refresh_token
        login_response = await client.post("/api/v1/auth/login", json={
            "username": "testuser",
            "password": "Test123456"
        })
        refresh_token = login_response.json()["refresh_token"]
        
        # 使用refresh_token获取新token
        response = await client.post("/api/v1/auth/refresh", json={
            "refresh_token": refresh_token
        })
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
    
    async def test_get_current_user(self, client: AsyncClient, test_user, auth_headers):
        """测试获取当前用户信息"""
        response = await client.get("/api/v1/auth/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["username"] == "testuser"
        assert data["email"] == "test@example.com"
    
    async def test_get_current_user_unauthorized(self, client: AsyncClient):
        """测试未授权访问"""
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 401
    
    async def test_change_password(self, client: AsyncClient, test_user, auth_headers):
        """测试修改密码"""
        response = await client.post("/api/v1/auth/change-password", 
            headers=auth_headers,
            json={
                "old_password": "Test123456",
                "new_password": "NewPass789",
                "confirm_password": "NewPass789"
            }
        )
        assert response.status_code == 200
        
        # 验证新密码可以登录
        login_response = await client.post("/api/v1/auth/login", json={
            "username": "testuser",
            "password": "NewPass789"
        })
        assert login_response.status_code == 200
    
    async def test_logout(self, client: AsyncClient, auth_headers):
        """测试登出"""
        response = await client.post("/api/v1/auth/logout", headers=auth_headers)
        assert response.status_code == 200
