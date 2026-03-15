"""
WebSocket 连接管理器

用于实时推送实例状态变化
"""
import asyncio
import json
from typing import Dict, Set, Optional
from fastapi import WebSocket
from datetime import datetime


class ConnectionManager:
    """WebSocket 连接管理器"""
    
    def __init__(self):
        # user_id -> set of WebSocket connections
        self.user_connections: Dict[str, Set[WebSocket]] = {}
        # instance_id -> set of WebSocket connections (订阅特定实例)
        self.instance_connections: Dict[str, Set[WebSocket]] = {}
        # 全局广播连接
        self.broadcast_connections: Set[WebSocket] = set()
    
    async def connect_user(self, websocket: WebSocket, user_id: str):
        """用户连接"""
        await websocket.accept()
        if user_id not in self.user_connections:
            self.user_connections[user_id] = set()
        self.user_connections[user_id].add(websocket)
    
    async def connect_instance(self, websocket: WebSocket, instance_id: str):
        """订阅特定实例状态"""
        await websocket.accept()
        if instance_id not in self.instance_connections:
            self.instance_connections[instance_id] = set()
        self.instance_connections[instance_id].add(websocket)
    
    def disconnect_user(self, websocket: WebSocket, user_id: str):
        """用户断开"""
        if user_id in self.user_connections:
            self.user_connections[user_id].discard(websocket)
            if not self.user_connections[user_id]:
                del self.user_connections[user_id]
    
    def disconnect_instance(self, websocket: WebSocket, instance_id: str):
        """取消订阅实例"""
        if instance_id in self.instance_connections:
            self.instance_connections[instance_id].discard(websocket)
            if not self.instance_connections[instance_id]:
                del self.instance_connections[instance_id]
    
    async def send_to_user(self, user_id: str, message: dict):
        """发送消息给特定用户的所有连接"""
        if user_id in self.user_connections:
            dead_connections = set()
            for connection in self.user_connections[user_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    dead_connections.add(connection)
            # 清理死连接
            for conn in dead_connections:
                self.user_connections[user_id].discard(conn)
    
    async def send_to_instance_subscribers(self, instance_id: str, message: dict):
        """发送消息给订阅特定实例的所有连接"""
        if instance_id in self.instance_connections:
            dead_connections = set()
            for connection in self.instance_connections[instance_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    dead_connections.add(connection)
            for conn in dead_connections:
                self.instance_connections[instance_id].discard(conn)


# 全局单例
ws_manager = ConnectionManager()


def get_ws_manager() -> ConnectionManager:
    """获取WebSocket管理器"""
    return ws_manager


async def broadcast_instance_status(
    instance_id: str, 
    user_id: str, 
    status: str, 
    extra_data: Optional[dict] = None
):
    """
    广播实例状态变化
    
    Args:
        instance_id: 实例ID
        user_id: 用户ID
        status: 新状态
        extra_data: 额外数据
    """
    message = {
        "type": "instance_status",
        "instance_id": instance_id,
        "status": status,
        "timestamp": datetime.utcnow().isoformat(),
    }
    if extra_data:
        message.update(extra_data)
    
    # 发送给用户
    await ws_manager.send_to_user(user_id, message)
    # 发送给实例订阅者
    await ws_manager.send_to_instance_subscribers(instance_id, message)


async def broadcast_billing_update(user_id: str, balance: float, event: str = "balance_update"):
    """广播账单/余额变化"""
    message = {
        "type": event,
        "balance": balance,
        "timestamp": datetime.utcnow().isoformat(),
    }
    await ws_manager.send_to_user(user_id, message)
