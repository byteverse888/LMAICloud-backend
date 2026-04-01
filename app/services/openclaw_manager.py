"""
OpenClaw 实例 K8s 资源管理器

管理 OpenClaw 实例的完整 K8s 资源生命周期:
  Namespace → Secret → ConfigMap → PVC → Deployment → Service

云端节点: 标准调度 + StorageClass PVC
边缘节点: 节点亲和 + 污点容忍 + LocalPV + 断网自治
"""
import json
import secrets
from datetime import datetime
from typing import Optional, Dict, Any, List

import yaml

from app.services.k8s_client import get_k8s_client
from app.services.pod_manager import PodManager


class OpenClawManager:
    """OpenClaw K8s 资源编排器"""

    # 资源名前缀
    PREFIX = "oc"

    def __init__(self):
        self.k8s = get_k8s_client()

    # ========== 工具方法 ==========

    @staticmethod
    def resource_name(instance_id: str) -> str:
        """统一资源命名: oc-{instance_id[:8]}"""
        return f"oc-{str(instance_id).replace('-', '')[:8]}"

    @staticmethod
    def generate_gateway_token() -> str:
        """生成随机 Gateway Token"""
        return secrets.token_urlsafe(32)

    @staticmethod
    def mask_api_key(key: str) -> str:
        """脱敏 API Key: sk-abc...xyz"""
        if not key or len(key) < 8:
            return "***"
        return f"{key[:6]}...{key[-4:]}"

    # ========== Secret 构建（敏感密钥） ==========

    def build_env_secret(
        self,
        instance_id: str,
        namespace: str,
        gateway_token: str,
        model_keys: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        构建 K8s Secret，内容为 .env 格式。
        挂载到 /home/node/.openclaw/.env
        """
        name = self.resource_name(instance_id)
        env_lines = [
            f"OPENCLAW_GATEWAY_TOKEN={gateway_token}",
        ]
        for mk in (model_keys or []):
            if not mk.get("is_active", True):
                continue
            provider = mk.get("provider", "").upper()
            api_key = mk.get("api_key", "")
            if provider and api_key:
                env_lines.append(f"{provider}_API_KEY={api_key}")
            base_url = mk.get("base_url")
            if base_url:
                env_lines.append(f"{provider}_BASE_URL={base_url}")

        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": f"{name}-env",
                "namespace": namespace,
                "labels": {
                    "app": "openclaw",
                    "openclaw-instance": str(instance_id),
                },
            },
            "type": "Opaque",
            "stringData": {
                ".env": "\n".join(env_lines),
            },
        }

    # ========== ConfigMap 构建（非敏感配置） ==========

    def build_config_map(
        self,
        instance_id: str,
        namespace: str,
        channels: Optional[List[Dict[str, Any]]] = None,
        skills: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        构建 ConfigMap，存储通道配置和 Skills 清单。
        """
        name = self.resource_name(instance_id)
        config_data = {
            "channels": [
                {"type": ch.get("type"), "name": ch.get("name"), "config": ch.get("config")}
                for ch in (channels or []) if ch.get("is_active", True)
            ],
            "skills": skills or [],
        }
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{name}-config",
                "namespace": namespace,
                "labels": {
                    "app": "openclaw",
                    "openclaw-instance": str(instance_id),
                },
            },
            "data": {
                "openclaw-config.json": json.dumps(config_data, ensure_ascii=False),
            },
        }

    # ========== PVC 构建 ==========

    def build_pvc(
        self,
        instance_id: str,
        namespace: str,
        disk_gb: int,
        node_type: str = "center",
        storage_class: str = "standard",
        edge_storage_path: str = "/opt/openclaw-data",
    ) -> Dict[str, Any]:
        """
        构建 PersistentVolumeClaim。
        - 云端: 使用 StorageClass
        - 边缘: 使用 hostPath (通过 local-path-provisioner 或直接 hostPath volume)
        """
        name = self.resource_name(instance_id)
        pvc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": f"{name}-data",
                "namespace": namespace,
                "labels": {
                    "app": "openclaw",
                    "openclaw-instance": str(instance_id),
                },
            },
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {
                    "requests": {
                        "storage": f"{disk_gb}Gi",
                    },
                },
            },
        }
        if node_type == "center":
            pvc["spec"]["storageClassName"] = storage_class
        else:
            # 边缘节点: 使用 local-path (如果有 provisioner) 或留空让集群默认处理
            pvc["spec"]["storageClassName"] = "local-path"
        return pvc

    # ========== Service 构建 ==========

    def build_service(
        self,
        instance_id: str,
        namespace: str,
        port: int = 18789,
    ) -> Dict[str, Any]:
        """构建 ClusterIP Service"""
        name = self.resource_name(instance_id)
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"{name}-svc",
                "namespace": namespace,
                "labels": {
                    "app": "openclaw",
                    "openclaw-instance": str(instance_id),
                },
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {
                    "openclaw-instance": str(instance_id),
                },
                "ports": [
                    {
                        "name": "gateway",
                        "port": port,
                        "targetPort": port,
                        "protocol": "TCP",
                    }
                ],
            },
        }

    # ========== Deployment 构建 ==========

    def build_deployment(
        self,
        instance_id: str,
        namespace: str,
        image_url: str,
        port: int = 18789,
        cpu_cores: int = 2,
        memory_gb: int = 4,
        node_name: Optional[str] = None,
        node_type: str = "center",
    ) -> Dict[str, Any]:
        """
        构建 OpenClaw Deployment。
        - Secret → .env 文件挂载
        - ConfigMap → 配置文件挂载
        - PVC → 持久化存储挂载
        - 健康检查: /healthz + /readyz
        """
        name = self.resource_name(instance_id)
        labels = {
            "app": "openclaw",
            "openclaw-instance": str(instance_id),
        }

        # 资源限制
        cpu_limit = max(1, min(cpu_cores, 8))
        cpu_request_m = max(250, cpu_limit * 250)
        mem_limit = max(1, min(memory_gb, 32))
        mem_request = max(1, mem_limit // 2)

        container = {
            "name": "openclaw",
            "image": image_url,
            "imagePullPolicy": "IfNotPresent",
            "ports": [{"containerPort": port, "name": "gateway"}],
            "resources": {
                "limits": {"cpu": str(cpu_limit), "memory": f"{mem_limit}Gi"},
                "requests": {"cpu": f"{cpu_request_m}m", "memory": f"{mem_request}Gi"},
            },
            "env": [
                {"name": "OPENCLAW_GATEWAY_BIND", "value": "lan"},
                {"name": "NODE_ENV", "value": "production"},
            ],
            "volumeMounts": [
                {
                    "name": "env-secret",
                    "mountPath": "/home/node/.openclaw/.env",
                    "subPath": ".env",
                    "readOnly": True,
                },
                {
                    "name": "config-volume",
                    "mountPath": "/home/node/.openclaw/config",
                    "readOnly": True,
                },
                {
                    "name": "data-volume",
                    "mountPath": "/home/node/.openclaw/workspace",
                },
            ],
            "livenessProbe": {
                "httpGet": {"path": "/healthz", "port": port},
                "initialDelaySeconds": 30,
                "periodSeconds": 30,
                "timeoutSeconds": 5,
                "failureThreshold": 3,
            },
            "readinessProbe": {
                "httpGet": {"path": "/readyz", "port": port},
                "initialDelaySeconds": 10,
                "periodSeconds": 10,
                "timeoutSeconds": 3,
                "failureThreshold": 3,
            },
        }

        volumes = [
            {
                "name": "env-secret",
                "secret": {"secretName": f"{name}-env"},
            },
            {
                "name": "config-volume",
                "configMap": {"name": f"{name}-config"},
            },
            {
                "name": "data-volume",
                "persistentVolumeClaim": {"claimName": f"{name}-data"},
            },
        ]

        # 节点调度
        tolerations = []
        node_selector = {}
        if node_type == "edge":
            node_selector["node-role.kubernetes.io/edge"] = ""
            tolerations.extend([
                {
                    "key": "node-role.kubernetes.io/edge",
                    "operator": "Exists",
                    "effect": "NoSchedule",
                },
                {
                    "key": "node.kubernetes.io/unreachable",
                    "operator": "Exists",
                    "effect": "NoExecute",
                    "tolerationSeconds": 120,
                },
            ])

        pod_spec = {
            "containers": [container],
            "volumes": volumes,
            "restartPolicy": "Always",
        }
        if tolerations:
            pod_spec["tolerations"] = tolerations
        if node_selector:
            pod_spec["nodeSelector"] = node_selector
        if node_name:
            pod_spec.pop("nodeSelector", None)
            pod_spec["nodeName"] = node_name

        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": f"{name}-deploy",
                "namespace": namespace,
                "labels": labels,
                "annotations": {
                    "openclaw/created-at": datetime.utcnow().isoformat(),
                    "openclaw/node-type": node_type,
                },
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"openclaw-instance": str(instance_id)}},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": pod_spec,
                },
            },
        }

    # ========== 生命周期方法 ==========

    def create_instance(
        self,
        instance_id: str,
        user_id: str,
        image_url: str,
        port: int = 18789,
        cpu_cores: int = 2,
        memory_gb: int = 4,
        disk_gb: int = 20,
        node_name: Optional[str] = None,
        node_type: str = "center",
        model_keys: Optional[List[Dict]] = None,
        channels: Optional[List[Dict]] = None,
        storage_class: str = "standard",
        edge_storage_path: str = "/opt/openclaw-data",
    ) -> Dict[str, Any]:
        """
        创建 OpenClaw 实例 — 按顺序创建全套 K8s 资源。

        Returns:
            {"success": bool, "namespace": str, "deployment_name": str,
             "service_name": str, "gateway_token": str, "error": str}
        """
        ns = PodManager.user_namespace(user_id)
        name = self.resource_name(instance_id)
        gateway_token = self.generate_gateway_token()

        try:
            # 1. 创建命名空间
            self.k8s.ensure_namespace(ns)

            # 2. 创建 Secret
            secret_spec = self.build_env_secret(instance_id, ns, gateway_token, model_keys)
            self.k8s.core_api.create_namespaced_secret(ns, secret_spec)

            # 3. 创建 ConfigMap
            cm_spec = self.build_config_map(instance_id, ns, channels)
            self.k8s.core_api.create_namespaced_config_map(ns, cm_spec)

            # 4. 创建 PVC
            pvc_spec = self.build_pvc(instance_id, ns, disk_gb, node_type, storage_class, edge_storage_path)
            self.k8s.core_api.create_namespaced_persistent_volume_claim(ns, pvc_spec)

            # 5. 创建 Deployment
            dep_spec = self.build_deployment(
                instance_id, ns, image_url, port, cpu_cores, memory_gb, node_name, node_type,
            )
            dep_name = self.k8s.create_deployment(ns, dep_spec)
            if not dep_name:
                return {"success": False, "error": "Failed to create Deployment"}

            # 6. 创建 Service
            svc_spec = self.build_service(instance_id, ns, port)
            self.k8s.create_service(ns, svc_spec)

            return {
                "success": True,
                "namespace": ns,
                "deployment_name": f"{name}-deploy",
                "service_name": f"{name}-svc",
                "gateway_token": gateway_token,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def start_instance(self, instance_id: str, namespace: str) -> bool:
        """启动实例 — scale replicas=1"""
        name = self.resource_name(instance_id)
        return self.k8s.scale_deployment(f"{name}-deploy", namespace, 1)

    def stop_instance(self, instance_id: str, namespace: str) -> bool:
        """停止实例 — scale replicas=0"""
        name = self.resource_name(instance_id)
        return self.k8s.scale_deployment(f"{name}-deploy", namespace, 0)

    def release_instance(self, instance_id: str, namespace: str) -> bool:
        """释放实例 — 级联删除全部 K8s 资源"""
        name = self.resource_name(instance_id)
        try:
            self.k8s.delete_deployment(f"{name}-deploy", namespace)
        except Exception:
            pass
        try:
            self.k8s.delete_service(f"{name}-svc", namespace)
        except Exception:
            pass
        try:
            self.k8s.core_api.delete_namespaced_secret(f"{name}-env", namespace)
        except Exception:
            pass
        try:
            self.k8s.core_api.delete_namespaced_config_map(f"{name}-config", namespace)
        except Exception:
            pass
        try:
            self.k8s.core_api.delete_namespaced_persistent_volume_claim(f"{name}-data", namespace)
        except Exception:
            pass
        return True

    def update_spec(
        self,
        instance_id: str,
        namespace: str,
        cpu_cores: Optional[int] = None,
        memory_gb: Optional[int] = None,
    ) -> bool:
        """变更资源规格 → 触发 Deployment 滚动更新"""
        name = self.resource_name(instance_id)
        dep = self.k8s.get_deployment(f"{name}-deploy", namespace)
        if not dep:
            return False

        # 构造 patch
        container_patch = {}
        if cpu_cores is not None:
            cpu_limit = max(1, min(cpu_cores, 8))
            container_patch.setdefault("resources", {}).setdefault("limits", {})["cpu"] = str(cpu_limit)
            container_patch.setdefault("resources", {}).setdefault("requests", {})["cpu"] = f"{max(250, cpu_limit * 250)}m"
        if memory_gb is not None:
            mem_limit = max(1, min(memory_gb, 32))
            container_patch.setdefault("resources", {}).setdefault("limits", {})["memory"] = f"{mem_limit}Gi"
            container_patch.setdefault("resources", {}).setdefault("requests", {})["memory"] = f"{max(1, mem_limit // 2)}Gi"

        if not container_patch:
            return True

        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{
                            "name": "openclaw",
                            **container_patch,
                        }]
                    }
                }
            }
        }
        return self.k8s.update_deployment(f"{name}-deploy", namespace, patch_body) is not None

    def hot_update_secret(
        self,
        instance_id: str,
        namespace: str,
        gateway_token: str,
        model_keys: List[Dict],
    ) -> bool:
        """热更新 Secret → 自动触发 Deployment 滚动重启"""
        name = self.resource_name(instance_id)
        secret_spec = self.build_env_secret(instance_id, namespace, gateway_token, model_keys)
        try:
            self.k8s.core_api.replace_namespaced_secret(f"{name}-env", namespace, secret_spec)
            # 触发滚动重启
            self.k8s.restart_deployment(f"{name}-deploy", namespace)
            return True
        except Exception:
            return False

    def hot_update_config(
        self,
        instance_id: str,
        namespace: str,
        channels: List[Dict],
        skills: List[str],
    ) -> bool:
        """热更新 ConfigMap → 触发 Deployment 滚动重启"""
        name = self.resource_name(instance_id)
        cm_spec = self.build_config_map(instance_id, namespace, channels, skills)
        try:
            self.k8s.core_api.replace_namespaced_config_map(f"{name}-config", namespace, cm_spec)
            self.k8s.restart_deployment(f"{name}-deploy", namespace)
            return True
        except Exception:
            return False

    def get_instance_pod_ip(self, instance_id: str, namespace: str) -> Optional[str]:
        """获取实例 Pod IP"""
        try:
            pods = self.k8s.list_pods(
                namespace,
                label_selector=f"openclaw-instance={instance_id}",
            )
            if pods and pods[0].get("ip"):
                return pods[0]["ip"]
        except Exception:
            pass
        return None


# 单例
_openclaw_manager: Optional[OpenClawManager] = None


def get_openclaw_manager() -> OpenClawManager:
    global _openclaw_manager
    if _openclaw_manager is None:
        _openclaw_manager = OpenClawManager()
    return _openclaw_manager
