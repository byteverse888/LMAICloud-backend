"""
OpenClaw Gateway REST API 客户端

通过 HTTP 调用运行中的 OpenClaw 实例 API，用于:
  - 监控任务检测实例健康状态
  - 查询通道连通性
  - 查询 Skills 列表
  - 获取 Gateway 版本/状态

OpenClaw Gateway 默认端口 18789，认证方式 Bearer Token。
API 文档参考: https://docs.openclaw.ai
"""
import asyncio
from typing import Optional, Dict, Any, List

import httpx


class OpenClawClient:
    """OpenClaw Gateway REST API 异步客户端"""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        """
        Args:
            base_url: OpenClaw Gateway 地址，如 http://oc-xxxx-svc.namespace.svc.cluster.local:18789
            token: Gateway Bearer Token
            timeout: 请求超时（秒）
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    # ========== 健康检查（无需认证） ==========

    async def check_health(self) -> bool:
        """GET /healthz — liveness 探针"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base_url}/healthz")
                return r.status_code == 200
        except Exception:
            return False

    async def check_ready(self) -> bool:
        """GET /readyz — readiness 探针"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base_url}/readyz")
                return r.status_code == 200
        except Exception:
            return False

    # ========== 状态查询 ==========

    async def get_status(self) -> Optional[Dict[str, Any]]:
        """
        GET /api/status — 获取 Gateway 状态

        Returns:
            {"status": "ok", "version": "1.9.2", "uptime": 3847, "sessions": 1}
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base_url}/api/status")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return None

    # ========== Sessions ==========

    async def list_sessions(self) -> List[Dict]:
        """GET /api/sessions — 列出所有会话"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base_url}/api/sessions", headers=self._headers())
                if r.status_code == 200:
                    return r.json() if isinstance(r.json(), list) else []
        except Exception:
            pass
        return []

    async def send_message(self, session_key: str, message: str) -> Optional[Dict]:
        """POST /api/sessions/{key}/messages — 向指定会话发送消息"""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{self.base_url}/api/sessions/{session_key}/messages",
                    headers=self._headers(),
                    json={"message": message},
                )
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return None

    # ========== Skills ==========

    async def list_skills(self) -> List[Dict]:
        """GET /api/skills — 列出已安装的技能"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base_url}/api/skills", headers=self._headers())
                if r.status_code == 200:
                    return r.json() if isinstance(r.json(), list) else []
        except Exception:
            pass
        return []

    async def install_skill(self, skill_name: str, version: Optional[str] = None) -> Optional[Dict]:
        """
        POST /api/skills — 安装技能
        OpenClaw Gateway 提供的 skill 安装 API。
        若 Gateway 不支持此接口，回退到配置文件方式。
        """
        try:
            body: Dict[str, Any] = {"name": skill_name}
            if version:
                body["version"] = version
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    f"{self.base_url}/api/skills",
                    headers=self._headers(),
                    json=body,
                )
                if r.status_code in (200, 201):
                    return r.json() if r.text else {"status": "ok"}
        except Exception:
            pass
        return None

    async def uninstall_skill(self, skill_name: str) -> bool:
        """
        DELETE /api/skills/{name} — 卸载技能
        若 Gateway 不支持此接口，回退到配置文件方式。
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.delete(
                    f"{self.base_url}/api/skills/{skill_name}",
                    headers=self._headers(),
                )
                return r.status_code in (200, 204)
        except Exception:
            return False

    # ========== Cron ==========

    async def list_cron_jobs(self) -> List[Dict]:
        """GET /api/cron — 列出定时任务"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base_url}/api/cron", headers=self._headers())
                if r.status_code == 200:
                    return r.json() if isinstance(r.json(), list) else []
        except Exception:
            pass
        return []

    # ========== Hooks ==========

    async def list_hooks(self) -> List[Dict]:
        """GET /api/hooks — 列出 Webhook"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base_url}/api/hooks", headers=self._headers())
                if r.status_code == 200:
                    return r.json() if isinstance(r.json(), list) else []
        except Exception:
            pass
        return []

    # ========== 综合检测 ==========

    async def full_health_check(self) -> Dict[str, Any]:
        """
        综合健康检测，返回完整状态快照。
        用于 ARQ 监控任务。
        """
        result = {
            "healthy": False,
            "ready": False,
            "version": None,
            "uptime": None,
            "sessions": None,
        }
        try:
            health_ok, ready_ok, status = await asyncio.gather(
                self.check_health(),
                self.check_ready(),
                self.get_status(),
                return_exceptions=True,
            )
            result["healthy"] = health_ok if isinstance(health_ok, bool) else False
            result["ready"] = ready_ok if isinstance(ready_ok, bool) else False
            if isinstance(status, dict):
                result["version"] = status.get("version")
                result["uptime"] = status.get("uptime")
                result["sessions"] = status.get("sessions")
        except Exception:
            pass
        return result


def build_openclaw_url(service_name: str, namespace: str, port: int = 18789) -> str:
    """
    构建集群内 OpenClaw Gateway URL。
    使用 K8s Service DNS: {service}.{namespace}.svc.cluster.local:{port}
    """
    return f"http://{service_name}.{namespace}.svc.cluster.local:{port}"
