"""
OpenClaw 实例 K8s 资源管理器（对齐官方 K8s 部署规范）

管理 OpenClaw 实例的完整 K8s 资源生命周期:
  Namespace → Secret → ConfigMap → PVC → Deployment → Service

参考: https://docs.openclaw.ai/install/kubernetes
      https://github.com/openclaw/openclaw/tree/main/scripts/k8s

云端节点: 标准调度 + StorageClass PVC
边缘节点: 节点亲和 + 污点容忍 + hostPath 直接挂载 + 断网自治
"""
import json
import secrets
from datetime import datetime
from typing import Optional, Dict, Any, List

import yaml

from app.services.k8s_client import get_k8s_client
from app.services.pod_manager import PodManager
from app.config import settings


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
        构建 K8s Secret（对齐官方 openclaw-secrets 结构）。
        每个密钥作为独立键值对，Deployment 通过 secretKeyRef 引用。
        """
        name = self.resource_name(instance_id)
        string_data = {
            "OPENCLAW_GATEWAY_TOKEN": gateway_token,
        }
        for mk in (model_keys or []):
            if not mk.get("is_active", True):
                continue
            provider = mk.get("provider", "").upper()
            api_key = mk.get("api_key", "")
            if provider and api_key:
                string_data[f"{provider}_API_KEY"] = api_key
            base_url = mk.get("base_url")
            if base_url:
                string_data[f"{provider}_BASE_URL"] = base_url

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
            "stringData": string_data,
        }

    # ========== ConfigMap 构建（非敏感配置） ==========

    def build_config_map(
        self,
        instance_id: str,
        namespace: str,
        channels: Optional[List[Dict[str, Any]]] = None,
        skills: Optional[List[str]] = None,
        port: int = 18789,
    ) -> Dict[str, Any]:
        """
        构建 ConfigMap（对齐官方 openclaw-config 结构）。
        包含:
        - openclaw.json: 网关配置（bind=lan, auth=token）
        - AGENTS.md: 默认 Agent 指令
        - openclaw-config.json: 通道/技能配置
        """
        name = self.resource_name(instance_id)

        # 官方网关配置
        gateway_config = {
            "gateway": {
                "mode": "local",
                "bind": "lan",       # 平台通过 Service 访问，需绑定局域网
                "port": port,
                "auth": {"mode": "token"},
                "controlUi": {"enabled": True},
            },
            "agents": {
                "defaults": {"workspace": "~/.openclaw/workspace"},
                "list": [
                    {
                        "id": "default",
                        "name": "OpenClaw Assistant",
                        "workspace": "~/.openclaw/workspace",
                    },
                ],
            },
            "cron": {"enabled": False},
        }

        # 通道/技能配置
        channel_config = {
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
                "openclaw.json": json.dumps(gateway_config, ensure_ascii=False),
                "AGENTS.md": "# OpenClaw Assistant\n\nYou are a helpful AI assistant running on LMAICloud.",
                "openclaw-config.json": json.dumps(channel_config, ensure_ascii=False),
            },
        }

    # ========== PV 构建（边缘节点专用） ==========

    def build_local_pv(
        self,
        instance_id: str,
        disk_gb: int,
        node_name: str,
        edge_storage_path: str = "/opt/openclaw-data",
    ) -> Dict[str, Any]:
        """
        构建边缘节点 PersistentVolume（hostPath + nodeAffinity）。
        - 每个实例独立目录: {edge_storage_path}/{instance_id}
        - nodeAffinity 绑定到指定边缘节点
        - 配合 no-provisioner StorageClass 静态供应
        """
        name = self.resource_name(instance_id)
        return {
            "apiVersion": "v1",
            "kind": "PersistentVolume",
            "metadata": {
                "name": f"{name}-data-pv",
                "labels": {
                    "app": "openclaw",
                    "openclaw-instance": str(instance_id),
                },
            },
            "spec": {
                "capacity": {"storage": f"{disk_gb}Gi"},
                "accessModes": ["ReadWriteOnce"],
                "persistentVolumeReclaimPolicy": "Delete",
                "storageClassName": settings.openclaw_edge_storage_class,
                "hostPath": {
                    "path": f"{edge_storage_path}/{instance_id}",
                    "type": "DirectoryOrCreate",
                },
                "nodeAffinity": {
                    "required": {
                        "nodeSelectorTerms": [{
                            "matchExpressions": [{
                                "key": "kubernetes.io/hostname",
                                "operator": "In",
                                "values": [node_name],
                            }],
                        }],
                    },
                },
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
    ) -> Dict[str, Any]:
        """
        构建 PersistentVolumeClaim。
        - 云端: 使用动态供应 StorageClass
        - 边缘: 使用 no-provisioner StorageClass（需先创建 PV）
        """
        name = self.resource_name(instance_id)
        is_edge = node_type != "center"
        sc = settings.openclaw_edge_storage_class if is_edge else storage_class
        spec: Dict[str, Any] = {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": sc,
            "resources": {
                "requests": {
                    "storage": f"{disk_gb}Gi",
                },
            },
        }
        # 边缘节点: 显式绑定到已创建的静态 PV
        if is_edge:
            spec["volumeName"] = f"{name}-data-pv"
        return {
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
            "spec": spec,
        }

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

    # ========== Deployment 构建（对齐官方 deployment.yaml） ==========

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
        构建 OpenClaw Deployment（对齐官方 deployment.yaml 结构）。
        - initContainer: busybox 复制 ConfigMap 到主目录
        - Secret: 通过 secretKeyRef 注入环境变量
        - 安全加固: readOnlyRootFilesystem, runAsNonRoot, drop ALL
        - 健康检查: exec 方式（官方标准）
        - 部署策略: Recreate
        """
        name = self.resource_name(instance_id)
        labels = {
            "app": "openclaw",
            "openclaw-instance": str(instance_id),
        }
        secret_name = f"{name}-env"

        # 资源限制: limits = 用户规格, requests = 规格的一半
        cpu_limit = max(1, min(cpu_cores, 8))
        cpu_request_m = max(250, cpu_limit * 500)
        mem_limit = max(1, min(memory_gb, 32))
        mem_request = max(1, mem_limit // 2) if mem_limit > 1 else 1

        # ── initContainer: 以 root 复制 ConfigMap 并设置权限 ──
        init_container = {
            "name": "init-config",
            "image": "busybox:1.37",
            "imagePullPolicy": "IfNotPresent",
            "command": [
                "sh", "-c",
                # 先 chown 确保目录可写（解决边缘节点 hostPath PV 初始权限问题）
                "chown -R 1000:1000 /home/node/.openclaw && "
                "cp /config/openclaw.json /home/node/.openclaw/openclaw.json && "
                "mkdir -p /home/node/.openclaw/workspace && "
                "cp /config/AGENTS.md /home/node/.openclaw/workspace/AGENTS.md && "
                "cp /config/openclaw-config.json /home/node/.openclaw/openclaw-config.json && "
                "chown -R 1000:1000 /home/node/.openclaw"
            ],
            "securityContext": {
                "runAsUser": 0,
                "runAsGroup": 0,
            },
            "resources": {
                "requests": {"memory": "32Mi", "cpu": "50m"},
                "limits": {"memory": "64Mi", "cpu": "100m"},
            },
            "volumeMounts": [
                {"name": "openclaw-home", "mountPath": "/home/node/.openclaw"},
                {"name": "config-volume", "mountPath": "/config"},
            ],
        }

        # ── 环境变量: 固定值 + secretKeyRef ──
        env_vars = [
            {"name": "HOME", "value": "/home/node"},
            {"name": "OPENCLAW_CONFIG_DIR", "value": "/home/node/.openclaw"},
            {"name": "NODE_ENV", "value": "production"},
            {
                "name": "OPENCLAW_GATEWAY_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {"name": secret_name, "key": "OPENCLAW_GATEWAY_TOKEN"},
                },
            },
        ]
        # 动态注入所有 provider API key（optional: true — Secret 中无此键时不报错）
        for provider in ["ANTHROPIC", "OPENAI", "GEMINI", "OPENROUTER"]:
            env_vars.append({
                "name": f"{provider}_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": secret_name,
                        "key": f"{provider}_API_KEY",
                        "optional": True,
                    },
                },
            })
            # BASE_URL 也支持（自定义端点）
            env_vars.append({
                "name": f"{provider}_BASE_URL",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": secret_name,
                        "key": f"{provider}_BASE_URL",
                        "optional": True,
                    },
                },
            })

        # ── 主容器: gateway ──
        healthz_cmd = (
            f"require('http').get('http://127.0.0.1:{port}/healthz',"
            f" r => process.exit(r.statusCode < 400 ? 0 : 1))"
            f".on('error', () => process.exit(1))"
        )
        readyz_cmd = (
            f"require('http').get('http://127.0.0.1:{port}/readyz',"
            f" r => process.exit(r.statusCode < 400 ? 0 : 1))"
            f".on('error', () => process.exit(1))"
        )

        container = {
            "name": "gateway",
            "image": image_url,
            "imagePullPolicy": "IfNotPresent",
            "ports": [{"containerPort": port, "name": "gateway", "protocol": "TCP"}],
            "resources": {
                "limits": {"cpu": str(cpu_limit), "memory": f"{mem_limit}Gi"},
                "requests": {"cpu": f"{cpu_request_m}m", "memory": f"{mem_request}Gi"},
            },
            "env": env_vars,
            "volumeMounts": [
                {"name": "openclaw-home", "mountPath": "/home/node/.openclaw"},
                {"name": "tmp-volume", "mountPath": "/tmp"},
            ],
            "livenessProbe": {
                "exec": {"command": ["node", "-e", healthz_cmd]},
                "initialDelaySeconds": 60,
                "periodSeconds": 30,
                "timeoutSeconds": 10,
                "failureThreshold": 3,
            },
            "readinessProbe": {
                "exec": {"command": ["node", "-e", readyz_cmd]},
                "initialDelaySeconds": 15,
                "periodSeconds": 10,
                "timeoutSeconds": 5,
                "failureThreshold": 3,
            },
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 1000,
                "runAsGroup": 1000,
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
        }

        # ── Volumes ──
        volumes = [
            {"name": "config-volume", "configMap": {"name": f"{name}-config"}},
            {"name": "tmp-volume", "emptyDir": {}},
        ]
        # 数据卷: 云端和边缘统一使用 PVC — 挂载到 /home/node/.openclaw
        volumes.append({
            "name": "openclaw-home",
            "persistentVolumeClaim": {"claimName": f"{name}-data"},
        })

        # ── 节点调度 ──
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

        # ── Pod Spec ──
        pod_spec = {
            "automountServiceAccountToken": False,
            "securityContext": {
                "fsGroup": 1000,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "initContainers": [init_container],
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
                "strategy": {"type": "Recreate"},
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
            self.k8s.core_v1.create_namespaced_secret(ns, secret_spec)

            # 3. 创建 ConfigMap
            cm_spec = self.build_config_map(instance_id, ns, channels, port=port)
            self.k8s.core_v1.create_namespaced_config_map(ns, cm_spec)

            # 4. 边缘节点: 先创建 PV（静态供应）
            if node_type == "edge":
                if not node_name:
                    return {"success": False, "error": "边缘节点必须指定 node_name"}
                pv_spec = self.build_local_pv(
                    instance_id, disk_gb, node_name, edge_storage_path,
                )
                self.k8s.create_pv(pv_spec)

            # 5. 创建 PVC（云端动态供应 / 边缘绑定已创建的 PV）
            pvc_spec = self.build_pvc(instance_id, ns, disk_gb, node_type, storage_class)
            self.k8s.core_v1.create_namespaced_persistent_volume_claim(ns, pvc_spec)

            # 6. 创建 Deployment
            dep_spec = self.build_deployment(
                instance_id, ns, image_url, port, cpu_cores, memory_gb, node_name, node_type,
            )
            dep_name = self.k8s.create_deployment(ns, dep_spec)
            if not dep_name:
                return {"success": False, "error": "Failed to create Deployment"}

            # 7. 创建 Service
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

    def release_instance(self, instance_id: str, namespace: str, node_type: str = "center") -> bool:
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
            self.k8s.core_v1.delete_namespaced_secret(f"{name}-env", namespace)
        except Exception:
            pass
        try:
            self.k8s.core_v1.delete_namespaced_config_map(f"{name}-config", namespace)
        except Exception:
            pass
        # PVC 和 PV 清理（云端和边缘都有 PVC）
        try:
            self.k8s.core_v1.delete_namespaced_persistent_volume_claim(f"{name}-data", namespace)
        except Exception:
            pass
        # 边缘节点额外清理 PV（PV 是集群级资源）
        if node_type == "edge":
            try:
                self.k8s.delete_pv(f"{name}-data-pv")
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
                            "name": "gateway",
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
            self.k8s.core_v1.replace_namespaced_secret(f"{name}-env", namespace, secret_spec)
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
        port: int = 18789,
    ) -> bool:
        """热更新 ConfigMap → 触发 Deployment 滚动重启"""
        name = self.resource_name(instance_id)
        cm_spec = self.build_config_map(instance_id, namespace, channels, skills, port=port)
        try:
            self.k8s.core_v1.replace_namespaced_config_map(f"{name}-config", namespace, cm_spec)
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
            if pods and pods[0].get("pod_ip"):
                return pods[0]["pod_ip"]
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
