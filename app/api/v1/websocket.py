"""
WebSocket 接口

提供实时的终端交互、日志流、状态推送功能
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import asyncio
import json
import uuid
import threading
from typing import Optional

from app.database import get_db, async_session_maker
from app.models import Instance, User, OpenClawInstance
from app.services.k8s_client import get_k8s_client
from app.services.ws_manager import get_ws_manager, ws_manager
from app.services.pod_manager import get_pod_manager
from app.utils.auth import decode_token
from app.logging_config import get_logger

router = APIRouter()
logger = get_logger("lmaicloud.websocket")


class TerminalManager:
    """终端会话管理器 - kubectl exec 模式"""
    
    def __init__(self):
        self.active_connections: dict = {}  # instance_id -> WebSocket
        self.exec_streams: dict = {}  # instance_id -> k8s exec stream
    
    async def connect(self, websocket: WebSocket, instance_id: str):
        await websocket.accept()
        self.active_connections[instance_id] = websocket
    
    def disconnect(self, instance_id: str):
        if instance_id in self.active_connections:
            del self.active_connections[instance_id]
        # 关闭 exec stream
        if instance_id in self.exec_streams:
            try:
                self.exec_streams[instance_id].close()
            except:
                pass
            del self.exec_streams[instance_id]
    
    async def send_message(self, instance_id: str, message: str):
        if instance_id in self.active_connections:
            await self.active_connections[instance_id].send_text(message)

    def create_exec_connection(self, pod_name: str, namespace: str, instance_id: str, shell: str = "/bin/sh") -> bool:
        """通过 kubectl exec 创建交互式连接"""
        try:
            k8s = get_k8s_client()
            resp = k8s.exec_interactive_stream(pod_name, namespace, [shell])
            if resp:
                self.exec_streams[instance_id] = resp
                logger.info(f"Exec连接成功 - 实例: {instance_id}, pod: {pod_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Exec连接失败 - 实例: {instance_id}, 错误: {e}")
            return False

    def send_to_exec(self, instance_id: str, data: str):
        """发送数据到 exec stream"""
        if instance_id in self.exec_streams:
            try:
                self.exec_streams[instance_id].write_stdin(data)
            except Exception as e:
                logger.error(f"Exec发送失败: {e}")

    def read_from_exec(self, instance_id: str, timeout: float = 0.3) -> Optional[str]:
        """从 exec stream 读取数据（阻塞式，需在线程中调用）
        返回值:
          - str (含数据): 有输出
          - ""  (空串):   流仍打开但暂无数据
          - None:         流已关闭 / 不存在
        """
        if instance_id not in self.exec_streams:
            return None
        try:
            resp = self.exec_streams[instance_id]
            if not resp.is_open():
                return None
            resp.update(timeout=timeout)
            output = ""
            if resp.peek_stdout():
                output += resp.read_stdout()
            if resp.peek_stderr():
                output += resp.read_stderr()
            return output   # 可能是 "" (暂无数据) 或实际输出
        except Exception as e:
            logger.error(f"read_from_exec 异常 - 实例: {instance_id}, 错误: {e}")
            return None

    def resize_exec(self, instance_id: str, cols: int, rows: int):
        """调整 exec 终端大小（K8s exec 不直接支持 resize，忽略）"""
        pass


terminal_manager = TerminalManager()


async def verify_websocket_token(token: str) -> User:
    """验证WebSocket连接的token"""
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            return None
        
        async with async_session_maker() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            return result.scalar_one_or_none()
    except Exception:
        return None


@router.websocket("/ws/terminal/{instance_id}")
async def websocket_terminal(
    websocket: WebSocket,
    instance_id: str,
    token: str = Query(...)
):
    """
    WebSocket 终端连接
    
    通过 kubectl exec 连接到 Pod 容器终端
    """
    # 验证 token
    user = await verify_websocket_token(token)
    if not user:
        await websocket.close(code=4001, reason="Invalid token")
        return
    
    # 验证实例归属
    async with async_session_maker() as session:
        result = await session.execute(
            select(Instance).where(
                Instance.id == instance_id,
                Instance.user_id == user.id
            )
        )
        instance = result.scalar_one_or_none()
        
        if not instance:
            await websocket.close(code=4004, reason="Instance not found")
            return
        
        if instance.status not in ("running", "error"):
            await websocket.close(code=4000, reason=f"Instance not available: {instance.status}")
            return
    
    # 为本次连接生成唯一 session_id，避免同一实例多连接互相覆盖
    session_id = f"{instance_id}:{uuid.uuid4().hex[:8]}"
    await terminal_manager.connect(websocket, session_id)
    
    try:
        # 直接使用 kubectl exec 模式
        k8s = get_k8s_client()
        namespace = instance.namespace or "lmaicloud"
        await websocket.send_json({"type": "info", "data": "正在查找 Pod..."})
        try:
            pods = await asyncio.to_thread(
                k8s.list_pods, namespace, f"instance-id={instance_id}"
            )
        except Exception as e:
            logger.error(f"K8s list_pods 异常: {e}")
            pods = []
        if not pods:
            await websocket.send_json({
                "type": "error",
                "data": "未找到关联的 Pod，K8s 连接可能异常"
            })
            return
        pod_name = pods[0]["name"]
        await websocket.send_json({"type": "info", "data": "正在建立终端连接..."})
        try:
            exec_ok = await asyncio.to_thread(
                terminal_manager.create_exec_connection,
                pod_name, namespace, session_id
            )
        except Exception as e:
            logger.error(f"create_exec_connection 异常: {e}")
            exec_ok = False
        if not exec_ok:
            await websocket.send_json({
                "type": "error",
                "data": "终端连接失败"
            })
            return

        # 发送连接成功消息
        await websocket.send_json({
            "type": "connected",
            "data": f"终端已连接 - 实例 {instance_id[:8]}"
        })
        
        # 启动输出读取任务
        async def read_output():
            while True:
                try:
                    output = await asyncio.to_thread(
                        terminal_manager.read_from_exec, session_id
                    )
                    if output is None:
                        # 流已关闭（shell 退出）
                        try:
                            await websocket.send_json({"type": "info", "data": "终端会话已结束"})
                            await websocket.close(code=1000, reason="Shell exited")
                        except: pass
                        break
                    if output:
                        await websocket.send_json({
                            "type": "output",
                            "data": output
                        })
                except Exception as e:
                    logger.error(f"read_output 异常 - 实例: {instance_id}, 错误: {e}")
                    break
        
        read_task = asyncio.create_task(read_output())
        
        # 消息循环
        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)
                
                if message.get("type") == "input":
                    await asyncio.to_thread(
                        terminal_manager.send_to_exec, session_id, message.get("data", "")
                    )
                    
                elif message.get("type") == "resize":
                    terminal_manager.resize_exec(
                        session_id,
                        message.get("cols", 120),
                        message.get("rows", 40)
                    )
                    
                elif message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        finally:
            read_task.cancel()
            
    except WebSocketDisconnect:
        logger.info(f"终端断开连接 - 实例: {instance_id}")
    except json.JSONDecodeError:
        pass
    except Exception as e:
        logger.error(f"终端错误 - 实例: {instance_id}, 错误: {e}")
        try:
            await websocket.send_json({"type": "error", "data": str(e)})
        except:
            pass
    finally:
        terminal_manager.disconnect(session_id)


@router.websocket("/ws/openclaw/terminal/{instance_id}")
async def websocket_openclaw_terminal(
    websocket: WebSocket,
    instance_id: str,
    token: str = Query(...)
):
    """
    OpenClaw 实例 WebSocket 终端连接

    通过 kubectl exec 连接到 OpenClaw Pod 容器终端
    """
    user = await verify_websocket_token(token)
    if not user:
        await websocket.close(code=4001, reason="Invalid token")
        return

    # 验证实例归属
    async with async_session_maker() as session:
        result = await session.execute(
            select(OpenClawInstance).where(
                OpenClawInstance.id == instance_id,
                OpenClawInstance.user_id == user.id
            )
        )
        instance = result.scalar_one_or_none()

        if not instance:
            await websocket.close(code=4004, reason="OpenClaw instance not found")
            return

        if instance.status not in ("running", "error"):
            await websocket.close(code=4000, reason=f"Instance not available: {instance.status}")
            return

    session_id = f"oc-{instance_id}:{uuid.uuid4().hex[:8]}"
    await terminal_manager.connect(websocket, session_id)

    try:
        k8s = get_k8s_client()
        namespace = instance.namespace or "lmaicloud"
        import time as _time
        await websocket.send_json({"type": "info", "data": "正在查找 Pod..."})
        _t0 = _time.monotonic()
        try:
            pods = await asyncio.to_thread(
                k8s.list_pods, namespace, f"openclaw-instance={instance_id}"
            )
        except Exception as e:
            logger.error(f"OpenClaw K8s list_pods 异常: {e}")
            pods = []
        _t1 = _time.monotonic()
        logger.info(f"OpenClaw 终端 list_pods 耗时: {_t1-_t0:.2f}s")
        if not pods:
            await websocket.send_json({
                "type": "error",
                "data": "未找到关联的 Pod，K8s 连接可能异常"
            })
            return
        pod_name = pods[0]["name"]
        await websocket.send_json({"type": "info", "data": "正在建立终端连接..."})
        try:
            exec_ok = await asyncio.to_thread(
                terminal_manager.create_exec_connection,
                pod_name, namespace, session_id
            )
        except Exception as e:
            logger.error(f"OpenClaw create_exec_connection 异常: {e}")
            exec_ok = False
        _t2 = _time.monotonic()
        logger.info(f"OpenClaw 终端 exec_connect 耗时: {_t2-_t1:.2f}s, 总耗时: {_t2-_t0:.2f}s")
        if not exec_ok:
            await websocket.send_json({
                "type": "error",
                "data": "终端连接失败"
            })
            return

        await websocket.send_json({
            "type": "connected",
            "data": f"终端已连接 - OpenClaw 实例 {instance_id[:8]}"
        })

        async def read_output():
            while True:
                try:
                    output = await asyncio.to_thread(
                        terminal_manager.read_from_exec, session_id
                    )
                    if output is None:
                        try:
                            await websocket.send_json({"type": "info", "data": "终端会话已结束"})
                            await websocket.close(code=1000, reason="Shell exited")
                        except: pass
                        break
                    if output:
                        await websocket.send_json({
                            "type": "output",
                            "data": output
                        })
                except Exception as e:
                    logger.error(f"OpenClaw read_output 异常 - 实例: {instance_id}, 错误: {e}")
                    break

        read_task = asyncio.create_task(read_output())

        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)

                if message.get("type") == "input":
                    await asyncio.to_thread(
                        terminal_manager.send_to_exec, session_id, message.get("data", "")
                    )
                elif message.get("type") == "resize":
                    terminal_manager.resize_exec(
                        session_id,
                        message.get("cols", 120),
                        message.get("rows", 40)
                    )
                elif message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        finally:
            read_task.cancel()

    except WebSocketDisconnect:
        logger.info(f"OpenClaw 终端断开连接 - 实例: {instance_id}")
    except json.JSONDecodeError:
        pass
    except Exception as e:
        logger.error(f"OpenClaw 终端错误 - 实例: {instance_id}, 错误: {e}")
        try:
            await websocket.send_json({"type": "error", "data": str(e)})
        except:
            pass
    finally:
        terminal_manager.disconnect(session_id)


@router.websocket("/ws/openclaw/admin/terminal/{instance_id}")
async def websocket_openclaw_admin_terminal(
    websocket: WebSocket,
    instance_id: str,
    token: str = Query(...)
):
    """
    管理端 OpenClaw 实例 WebSocket 终端连接

    管理员可连接任意 OpenClaw 实例终端（不检查 user_id 归属）
    """
    user = await verify_websocket_token(token)
    if not user:
        await websocket.close(code=4001, reason="Invalid token")
        return

    # 验证管理员权限
    if user.role != "admin":
        await websocket.close(code=4003, reason="Admin required")
        return

    # 查找实例（不限制 user_id）
    async with async_session_maker() as session:
        result = await session.execute(
            select(OpenClawInstance).where(OpenClawInstance.id == instance_id)
        )
        instance = result.scalar_one_or_none()

        if not instance:
            await websocket.close(code=4004, reason="OpenClaw instance not found")
            return

        if instance.status not in ("running", "error"):
            await websocket.close(code=4000, reason=f"Instance not available: {instance.status}")
            return

    session_id = f"oc-admin-{instance_id}:{uuid.uuid4().hex[:8]}"
    await terminal_manager.connect(websocket, session_id)

    try:
        k8s = get_k8s_client()
        namespace = instance.namespace or "lmaicloud"
        try:
            pods = k8s.list_pods(namespace, label_selector=f"openclaw-instance={instance_id}")
        except Exception as e:
            logger.error(f"Admin OpenClaw K8s list_pods 异常: {e}")
            pods = []
        if not pods:
            await websocket.send_json({
                "type": "error",
                "data": "未找到关联的 Pod，K8s 连接可能异常"
            })
            return
        pod_name = pods[0]["name"]
        try:
            exec_ok = terminal_manager.create_exec_connection(
                pod_name=pod_name, namespace=namespace, instance_id=session_id
            )
        except Exception as e:
            logger.error(f"Admin OpenClaw create_exec_connection 异常: {e}")
            exec_ok = False
        if not exec_ok:
            await websocket.send_json({
                "type": "error",
                "data": "终端连接失败"
            })
            return

        await websocket.send_json({
            "type": "connected",
            "data": f"[管理端] 终端已连接 - OpenClaw 实例 {instance_id[:8]}"
        })

        async def read_output():
            while True:
                try:
                    output = await asyncio.to_thread(
                        terminal_manager.read_from_exec, session_id
                    )
                    if output is None:
                        try:
                            await websocket.send_json({"type": "info", "data": "终端会话已结束"})
                            await websocket.close(code=1000, reason="Shell exited")
                        except: pass
                        break
                    if output:
                        await websocket.send_json({
                            "type": "output",
                            "data": output
                        })
                except Exception as e:
                    logger.error(f"Admin OpenClaw read_output 异常 - 实例: {instance_id}, 错误: {e}")
                    break

        read_task = asyncio.create_task(read_output())

        try:
            while True:
                data = await websocket.receive_text()
                message = json.loads(data)

                if message.get("type") == "input":
                    await asyncio.to_thread(
                        terminal_manager.send_to_exec, session_id, message.get("data", "")
                    )
                elif message.get("type") == "resize":
                    terminal_manager.resize_exec(
                        session_id,
                        message.get("cols", 120),
                        message.get("rows", 40)
                    )
                elif message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        finally:
            read_task.cancel()

    except WebSocketDisconnect:
        logger.info(f"Admin OpenClaw 终端断开连接 - 实例: {instance_id}")
    except json.JSONDecodeError:
        pass
    except Exception as e:
        logger.error(f"Admin OpenClaw 终端错误 - 实例: {instance_id}, 错误: {e}")
        try:
            await websocket.send_json({"type": "error", "data": str(e)})
        except:
            pass
    finally:
        terminal_manager.disconnect(session_id)


@router.websocket("/ws/logs/{instance_id}")
async def websocket_logs(
    websocket: WebSocket,
    instance_id: str,
    token: str = Query(...),
    follow: bool = Query(default=True),
    tail: int = Query(default=100)
):
    """
    WebSocket 日志流
    
    实时推送 Pod 日志
    
    参数:
    - follow: 是否实时跟踪新日志（默认true）
    - tail: 初始获取最后多少行日志（默认100）
    """
    # 验证 token
    user = await verify_websocket_token(token)
    if not user:
        await websocket.close(code=4001, reason="Invalid token")
        return
    
    # 验证实例归属
    async with async_session_maker() as session:
        result = await session.execute(
            select(Instance).where(
                Instance.id == instance_id,
                Instance.user_id == user.id
            )
        )
        instance = result.scalar_one_or_none()
        
        if not instance:
            await websocket.close(code=4004, reason="Instance not found")
            return
    
    await websocket.accept()
    logger.info(f"日志流连接 - 实例: {instance_id}, follow: {follow}, tail: {tail}")
    
    try:
        k8s = get_k8s_client()
        namespace = instance.namespace or "lmaicloud"
        
        # 通过 label selector 查找 Deployment 关联的 Pod（Pod 名由 ReplicaSet 随机后缀生成）
        try:
            pods = k8s.list_pods(namespace, label_selector=f"instance-id={instance_id}")
        except Exception as e:
            logger.error(f"日志流 list_pods 异常: {e}")
            pods = []
        pod_name = pods[0]["name"] if pods else None
        
        # 发送连接成功消息
        await websocket.send_json({
            "type": "connected",
            "data": f"日志流已连接 - {pod_name or '(等待 Pod 启动)'}"
        })
        
        if not pod_name:
            await websocket.send_json({
                "type": "error",
                "data": "未找到关联的 Pod，请确认实例已启动"
            })
            # 仍保持连接，循环等待 Pod 出现
        
        # 获取初始日志
        try:
            initial_logs = k8s.get_pod_logs(pod_name, namespace, tail_lines=tail) if pod_name else None
        except Exception:
            initial_logs = None
        if initial_logs:
            await websocket.send_json({
                "type": "log",
                "data": initial_logs
            })
        
        if not follow:
            await websocket.close()
            return
        
        # 持续推送新日志
        last_log_hash = hash(initial_logs) if initial_logs else 0
        
        while True:
            try:
                # 检查客户端消息（ping等）
                try:
                    message = await asyncio.wait_for(
                        websocket.receive_text(), 
                        timeout=0.1
                    )
                    data = json.loads(message)
                    if data.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                except asyncio.TimeoutError:
                    pass
                
                # 获取新日志（动态刷新 pod_name，Pod 可能重建）
                if not pod_name:
                    try:
                        pods = k8s.list_pods(namespace, label_selector=f"instance-id={instance_id}")
                        pod_name = pods[0]["name"] if pods else None
                    except Exception:
                        pass
                
                try:
                    logs = k8s.get_pod_logs(pod_name, namespace, tail_lines=tail) if pod_name else None
                except Exception:
                    logs = None
                if logs:
                    current_hash = hash(logs)
                    if current_hash != last_log_hash:
                        # 计算新增的日志
                        await websocket.send_json({
                            "type": "log",
                            "data": logs
                        })
                        last_log_hash = current_hash
                
                await asyncio.sleep(1)  # 每秒检查一次
                
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"日志流错误: {e}")
                await websocket.send_json({
                    "type": "error",
                    "data": str(e)
                })
                await asyncio.sleep(2)
                
    except WebSocketDisconnect:
        logger.info(f"日志流断开 - 实例: {instance_id}")
    except Exception as e:
        logger.error(f"日志流异常 - 实例: {instance_id}, 错误: {e}")


@router.websocket("/ws/status")
async def websocket_status(
    websocket: WebSocket,
    token: str = Query(...)
):
    """
    WebSocket 状态订阅
    
    用户连接后可接收所有实例状态变化推送
    
    消息类型:
    - instance_status: 实例状态变化 {type, instance_id, status, timestamp, ...}
    - balance_update: 余额变化 {type, balance, timestamp}
    """
    user = await verify_websocket_token(token)
    if not user:
        await websocket.close(code=4001, reason="Invalid token")
        return
    
    user_id = str(user.id)
    await ws_manager.connect_user(websocket, user_id)
    
    try:
        # 发送连接成功消息
        await websocket.send_json({
            "type": "connected",
            "message": "Status subscription connected",
            "user_id": user_id
        })
        
        # 保持连接，等待推送消息
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)
                
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                elif message.get("type") == "subscribe_instance":
                    # 订阅特定实例状态
                    instance_id = message.get("instance_id")
                    if instance_id:
                        if instance_id not in ws_manager.instance_connections:
                            ws_manager.instance_connections[instance_id] = set()
                        ws_manager.instance_connections[instance_id].add(websocket)
                        await websocket.send_json({
                            "type": "subscribed",
                            "instance_id": instance_id
                        })
            except json.JSONDecodeError:
                pass
                
    except WebSocketDisconnect:
        ws_manager.disconnect_user(websocket, user_id)
    except Exception as e:
        ws_manager.disconnect_user(websocket, user_id)


@router.websocket("/ws/instance/{instance_id}/status")
async def websocket_instance_status(
    websocket: WebSocket,
    instance_id: str,
    token: str = Query(...)
):
    """
    WebSocket 单实例状态订阅
    
    订阅特定实例的状态变化
    """
    user = await verify_websocket_token(token)
    if not user:
        await websocket.close(code=4001, reason="Invalid token")
        return
    
    # 验证实例归属
    async with async_session_maker() as session:
        result = await session.execute(
            select(Instance).where(
                Instance.id == instance_id,
                Instance.user_id == user.id
            )
        )
        instance = result.scalar_one_or_none()
        
        if not instance:
            await websocket.close(code=4004, reason="Instance not found")
            return
    
    await ws_manager.connect_instance(websocket, instance_id)
    
    try:
        await websocket.send_json({
            "type": "connected",
            "instance_id": instance_id,
            "current_status": instance.status
        })
        
        while True:
            try:
                data = await websocket.receive_text()
                message = json.loads(data)
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except json.JSONDecodeError:
                pass
                
    except WebSocketDisconnect:
        ws_manager.disconnect_instance(websocket, instance_id)
    except Exception:
        ws_manager.disconnect_instance(websocket, instance_id)
