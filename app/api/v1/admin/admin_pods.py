"""管理后台 - 容器(Pod)管理 API"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from typing import Optional
import json
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils.auth import get_current_admin_user
from app.services.k8s_client import get_k8s_client
from app.logging_config import get_logger
from app.database import get_db
from app.api.v1.audit_log import create_audit_log, get_client_ip
from app.models import AuditAction, AuditResourceType

router = APIRouter()
logger = get_logger("lmaicloud.admin_pods")


@router.get("", summary="获取 Pod 列表")
async def list_pods(
    namespace: Optional[str] = Query(None, description="命名空间，为空则查全部"),
    node: Optional[str] = Query(None, description="按节点过滤"),
    status: Optional[str] = Query(None, description="按状态过滤: Running/Pending/Succeeded/Failed"),
    search: Optional[str] = Query(None, description="名称搜索"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    if not k8s.is_connected:
        return {"list": [], "total": 0}

    all_ns = namespace is None or namespace == ""
    try:
        pods = k8s.list_pods(
            namespace=namespace or "default",
            all_namespaces=all_ns,
        )
    except Exception as e:
        print(f"[API] list_pods error: {e}")
        pods = []

    # 从 Deployment 批量获取 instance_name，补充到 Pod 上
    # （兼容旧版 Deployment 没有在 pod template 里写 annotation 的情况）
    try:
        deps = k8s.list_deployments(
            namespace=namespace or "default",
            all_namespaces=all_ns,
        )
        dep_name_map = {}
        for d in deps:
            iname = d.get("instance_name") or ""
            if iname:
                dep_name_map[d["name"]] = iname
        for p in pods:
            if not p.get("instance_name"):
                # Pod 名格式: {deployment_name}-{replicaset_hash}-{random}
                # 尝试匹配 deployment name 前缀
                pod_name = p["name"]
                for dep_n, inst_n in dep_name_map.items():
                    if pod_name.startswith(dep_n + "-"):
                        p["instance_name"] = inst_n
                        break
    except Exception:
        pass  # 补充失败不影响主列表

    # 搜索：同时匹配 Pod 名称和实例名称
    if search:
        s = search.lower()
        pods = [p for p in pods if s in p["name"].lower() or s in (p.get("instance_name") or "").lower()]
    if node:
        pods = [p for p in pods if p.get("node_name") == node]
    if status:
        # 优先用 effective_status 过滤（含 Terminating/starting），
        # fallback 到原始 phase（status 字段）
        def _match_status(p: dict) -> bool:
            eff = p.get("effective_status") or p.get("status", "")
            raw = p.get("status", "")
            # 过滤值与 effective_status 匹配（大小写不敏感）
            if eff.lower() == status.lower():
                return True
            # 过滤值与原始 phase 匹配，但排除已被标记为 Terminating 的 pod
            if raw.lower() == status.lower() and eff.lower() != "terminating":
                return True
            return False
        pods = [p for p in pods if _match_status(p)]

    total = len(pods)
    # 前端做客户端分页，后端返回全量数据
    # 合并 Pod metrics（CPU/内存使用量）
    try:
        all_ns_flag = namespace is None or namespace == ""
        pod_metrics = k8s.list_pod_metrics(
            namespace=namespace or "default",
            all_namespaces=all_ns_flag,
        )
        # 构建 metrics map: "namespace/name" -> metrics
        metrics_map = {}
        for m in pod_metrics:
            key = f"{m['namespace']}/{m['name']}"
            metrics_map[key] = m
        for p in pods:
            key = f"{p.get('namespace', '')}/{p.get('name', '')}"
            pm = metrics_map.get(key)
            if pm:
                p["cpu_usage_millicores"] = pm["cpu_usage_millicores"]
                p["memory_usage_bytes"] = pm["memory_usage_bytes"]
            else:
                p["cpu_usage_millicores"] = None
                p["memory_usage_bytes"] = None
    except Exception:
        pass  # metrics 获取失败不影响主列表

    return {"list": pods, "total": total}


@router.get("/{ns}/{name}", summary="获取 Pod 详情")
async def get_pod(
    ns: str, name: str,
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    pod = k8s.get_pod(name, ns)
    if not pod:
        raise HTTPException(status_code=404, detail=f"Pod {ns}/{name} 不存在")
    # 附加事件信息
    pod["events"] = k8s.get_pod_events(name, ns)
    return pod


@router.delete("/{ns}/{name}", summary="删除 Pod")
async def delete_pod(
    ns: str, name: str,
    request: Request,
    current_user=Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    k8s = get_k8s_client()
    # 先查询 Pod 状态：若为 Terminating（deletion_timestamp 不为空）则强制删除（grace_period=0）
    pod_info = k8s.get_pod(name, ns)
    is_terminating = pod_info.get("is_terminating", False) if pod_info else False
    ok = k8s.delete_pod(name, ns, force=is_terminating)
    if not ok:
        raise HTTPException(status_code=400, detail="删除 Pod 失败")
    detail = f"管理端{'强制' if is_terminating else ''}删除 Pod {ns}/{name}"
    await create_audit_log(
        db, current_user.id, AuditAction.DELETE, AuditResourceType.INSTANCE,
        resource_id=f"{ns}/{name}", resource_name=name,
        detail=detail,
        ip_address=get_client_ip(request),
    )
    await db.commit()
    return {"message": "删除成功"}


@router.get("/{ns}/{name}/logs", summary="获取 Pod 日志")
async def get_pod_logs(
    ns: str, name: str,
    container: Optional[str] = Query(None, description="容器名称"),
    tail: int = Query(200, ge=1, le=5000, description="尾部行数"),
    current_user=Depends(get_current_admin_user),
):
    k8s = get_k8s_client()
    try:
        logs = k8s.get_pod_logs(name, ns, tail_lines=tail, container=container)
    except Exception as e:
        print(f"[API] get_pod_logs error: {e}")
        logs = None
    if logs is None:
        raise HTTPException(status_code=404, detail="获取日志失败")
    return {"logs": logs}


# ========== WebSocket: Pod Exec 终端 ==========

@router.websocket("/ws/{ns}/{name}/exec")
async def pod_exec_terminal(ws: WebSocket, ns: str, name: str):
    """管理端 Pod exec 终端 WebSocket"""
    await ws.accept()

    # 从 query 获取 token 验证身份
    token = ws.query_params.get("token")
    if not token:
        await ws.send_json({"type": "error", "data": "缺少认证 token"})
        await ws.close()
        return

    # 验证 admin 权限
    from app.utils.auth import decode_token
    from app.database import async_session_maker
    from app.models import User
    from sqlalchemy import select
    try:
        payload = decode_token(token)
        if not payload:
            await ws.send_json({"type": "error", "data": "token 无效"})
            await ws.close()
            return
        user_id = payload.get("sub")
        async with async_session_maker() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
        if not user or (str(getattr(user.role, 'value', user.role)) != "admin"):
            await ws.send_json({"type": "error", "data": "需要管理员权限"})
            await ws.close()
            return
    except Exception:
        await ws.send_json({"type": "error", "data": "token 无效"})
        await ws.close()
        return

    container = ws.query_params.get("container")
    k8s = get_k8s_client()

    # 检测 Pod 是否存在（在线程中运行避免阻塞事件循环）
    pod_info = await asyncio.to_thread(k8s.get_pod, name, ns)
    if not pod_info:
        await ws.send_json({"type": "error", "data": f"Pod {ns}/{name} 不存在"})
        await ws.close()
        return

    # 检测可用 shell（边缘节点通过 CloudCore 隧道延迟较大，需容错+超时保护）
    # 优化策略：优先检测 /bin/sh，如果能连接直接使用；
    # 如果超时说明网络慢而非 shell 不存在，跳过 /bin/bash 检测直接用 /bin/sh
    shell_cmd = ["/bin/sh"]  # 默认 fallback
    for sh in ["/bin/sh", "/bin/bash"]:
        try:
            test = await asyncio.wait_for(
                asyncio.to_thread(
                    k8s.exec_in_pod, name, ns, [sh, "-c", "echo __shell_ok__"], container
                ),
                timeout=5,
            )
            if test is not None and "__shell_ok__" in test:
                shell_cmd = [sh]
                break
        except asyncio.TimeoutError:
            logger.warning(f"shell 检测超时 - pod: {ns}/{name}, shell: {sh}")
            if sh == "/bin/sh":
                # /bin/sh 超时说明网络慢，跳过后续检测直接用 /bin/sh
                break
        except Exception as e:
            logger.warning(f"shell 检测失败 - pod: {ns}/{name}, shell: {sh}, 错误: {e}")

    try:
        stream = await asyncio.wait_for(
            asyncio.to_thread(
                k8s.exec_interactive_stream, name, ns, shell_cmd, container
            ),
            timeout=15,
        )
    except asyncio.TimeoutError:
        logger.error(f"exec stream 连接超时 - pod: {ns}/{name}")
        stream = None
    except Exception as e:
        logger.error(f"exec stream 连接失败 - pod: {ns}/{name}, 错误: {e}")
        stream = None
    if not stream:
        await ws.send_json({"type": "error", "data": "无法连接到容器终端"})
        await ws.close()
        return

    await ws.send_json({"type": "connected", "data": f"已连接 {ns}/{name}"})
    last_resize = {"Width": 80, "Height": 24}

    async def read_stream():
        """从 K8s stream 读取输出发送到 WebSocket（线程安全）"""
        try:
            while True:
                def _poll():
                    if not stream.is_open():
                        return None  # 流已关闭
                    stream.update(timeout=0.5)

                    # 排空紧随的 status / close 帧
                    for _ in range(5):
                        if not stream.is_open() or getattr(stream, 'returncode', None) is not None:
                            break
                        try:
                            stream.update(timeout=0.01)
                        except Exception:
                            break

                    if not stream.is_open() or getattr(stream, 'returncode', None) is not None:
                        out = ""
                        if stream.peek_stdout():
                            out += stream.read_stdout()
                        if stream.peek_stderr():
                            out += stream.read_stderr()
                        return out if out else None

                    out = ""
                    if stream.peek_stdout():
                        out += stream.read_stdout()
                    if stream.peek_stderr():
                        out += stream.read_stderr()

                    # 写探测：无输出时通过 resize channel 触发服务端检测死连接
                    # channel 4 是 RESIZE_CHANNEL，不会产生可见输出
                    if not out:
                        try:
                            import json as _j
                            stream.write_channel(4, _j.dumps(last_resize))
                        except Exception:
                            return None
                        try:
                            stream.update(timeout=0.15)
                        except Exception:
                            return None
                        if not stream.is_open() or getattr(stream, 'returncode', None) is not None:
                            remaining = ""
                            if stream.peek_stdout():
                                remaining += stream.read_stdout()
                            if stream.peek_stderr():
                                remaining += stream.read_stderr()
                            return remaining if remaining else None

                    return out if out else ""

                data = await asyncio.to_thread(_poll)
                if data is None:
                    logger.info(f"read_stream 流已关闭 - pod: {ns}/{name}")
                    try:
                        await ws.send_json({"type": "info", "data": "终端会话已结束"})
                        await ws.close(code=1000, reason="Shell exited")
                    except Exception:
                        pass
                    break
                if data:
                    await ws.send_json({"type": "output", "data": data})
        except Exception as e:
            logger.error(f"read_stream 异常 - pod: {ns}/{name}, 错误: {e}")

    read_task = asyncio.create_task(read_stream())

    try:
        while True:
            msg = await ws.receive_text()
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                data = {"type": "input", "data": msg}

            if data.get("type") == "input":
                if stream.is_open():
                    await asyncio.to_thread(stream.write_stdin, data.get("data", ""))
            elif data.get("type") == "resize":
                # K8s exec resize 通过 channel 4
                cols = data.get("cols", 80)
                rows = data.get("rows", 24)
                last_resize.update({"Width": cols, "Height": rows})
                try:
                    import json as _json
                    resize_msg = _json.dumps({"Width": cols, "Height": rows})
                    await asyncio.to_thread(stream.write_channel, 4, resize_msg)
                except Exception:
                    pass
            elif data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        read_task.cancel()
        def _cleanup_stream():
            import socket as _sock
            try:
                if stream.is_open():
                    try:
                        stream.write_stdin("\x03")
                        import time as _time
                        _time.sleep(0.1)
                        stream.write_stdin("\nexit\n")
                        stream.write_stdin("\x04")
                    except Exception:
                        pass
                    for _ in range(30):
                        if not stream.is_open() or getattr(stream, 'returncode', None) is not None:
                            break
                        try:
                            stream.update(timeout=0.1)
                        except Exception:
                            break
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
            # 确保底层 TCP 彻底关闭
            try:
                ws_s = getattr(stream, 'sock', None)
                if ws_s:
                    raw = getattr(ws_s, 'sock', None)
                    if raw:
                        try:
                            raw.shutdown(_sock.SHUT_RDWR)
                        except OSError:
                            pass
                        try:
                            raw.close()
                        except OSError:
                            pass
            except Exception:
                pass
        await asyncio.to_thread(_cleanup_stream)
