"""
GPU Pod 管理服务

负责创建、管理GPU实例对应的K8s资源（Deployment）
用户输入整合为 deployment.yaml，通过 K8s API 下发调度。
YAML 中通过 nodeSelector 区分边缘节点(edge) / 中心节点(center)。
"""
import json
import os
from datetime import datetime
from typing import Optional, Dict, Any, List

import yaml

from app.services.k8s_client import get_k8s_client


class PodManager:
    """GPU Pod 管理器 - 基于 Deployment"""

    NAMESPACE_PREFIX = "lmai"  # 命名空间前缀

    def __init__(self):
        self.k8s = get_k8s_client()

    @staticmethod
    def user_namespace(user_id: str) -> str:
        """根据用户ID生成 K8s 命名空间名称: lmai-{user_id[:8]}"""
        return f"lmai-{str(user_id).replace('-', '')[:8]}"

    # ========== YAML 构建 ==========

    def build_deployment_yaml(
        self,
        instance_id: str,
        instance_name: str,
        user_id: str,
        image: str,
        gpu_count: int,
        cpu_cores: int,
        memory_gb: int,
        disk_gb: int,
        node_name: Optional[str] = None,
        node_type: str = "center",
        env_vars: Optional[List[Dict[str, str]]] = None,
        startup_command: Optional[str] = None,
        storage_mounts: Optional[List[Dict[str, Any]]] = None,
        instance_count: int = 1,
        pip_source: str = "default",
        conda_source: str = "default",
        apt_source: str = "default",
        namespace: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        构建完整的 Deployment YAML

        nodeSelector 通过 node-role 标签区分:
          - center 节点: nodeSelector: { "node-role": "center" }
          - edge 节点:   nodeSelector: { "node-role": "edge" }
        同时对 edge 节点增加 toleration 以适配 KubeEdge 边缘场景。
        """
        short_id = instance_id[:8]
        # 限制实例数量，防止创建过多副本导致 OOM 循环
        instance_count = max(1, min(instance_count, 5))
        labels = {
            "app": "gpu-instance",
            "instance-id": instance_id,
            "user-id": user_id,
            "node-type": node_type,
        }

        # --- 资源限制 ---
        # CPU 用毫核(m)，合理分配而非占满整个节点
        # limits 取传入值但设上限，requests 设为较小值保证可调度
        cpu_limit = min(cpu_cores, 4)      # 单 Pod 最高 4 核
        cpu_request_m = max(100, min(cpu_cores * 250, 2000))  # 250m/核，最低100m 最高2000m
        mem_limit = min(memory_gb, 16)     # 单 Pod 最高 16Gi
        mem_request = max(1, mem_limit // 4)  # requests = limits/4，至少 1Gi

        resources = {
            "limits": {
                "cpu": str(cpu_limit),
                "memory": f"{mem_limit}Gi",
            },
            "requests": {
                "cpu": f"{cpu_request_m}m",
                "memory": f"{mem_request}Gi",
            }
        }
        if gpu_count > 0:
            resources["limits"]["nvidia.com/gpu"] = str(gpu_count)
            resources["requests"]["nvidia.com/gpu"] = str(gpu_count)

        # --- 环境变量 ---
        env_list = [
            {"name": "INSTANCE_ID", "value": instance_id},
            {"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
            {"name": "PIP_SOURCE", "value": pip_source},
            {"name": "CONDA_SOURCE", "value": conda_source},
            {"name": "APT_SOURCE", "value": apt_source},
        ]
        if env_vars:
            for ev in env_vars:
                env_list.append({"name": ev["key"], "value": ev["value"]})

        # --- 启动命令 ---
        if startup_command:
            full_cmd = startup_command
        else:
            full_cmd = "while true; do sleep 3600; done"

        # --- 容器配置 ---
        container = {
            "name": "gpu-container",
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "resources": resources,
            "env": env_list,
        }

        # 启动命令：统一用 command 字段传递完整命令，避免 args 为空导致 sh -c "" 退出
        if full_cmd:
            container["command"] = ["/bin/sh", "-c", full_cmd]

        # --- 数据卷 ---
        volumes = []
        volume_mounts = []
        # 默认数据卷
        volumes.append({
            "name": "instance-data",
            "emptyDir": {"sizeLimit": f"{disk_gb}Gi"},
        })
        volume_mounts.append({
            "name": "instance-data",
            "mountPath": "/root/data",
        })
        # 用户自定义存储挂载
        if storage_mounts:
            for i, sm in enumerate(storage_mounts):
                vol_name = sm.get("name", f"vol-{i}")
                volumes.append({
                    "name": vol_name,
                    "emptyDir": {"sizeLimit": f"{sm.get('size_gb', 50)}Gi"},
                })
                volume_mounts.append({
                    "name": vol_name,
                    "mountPath": sm.get("mount_path", f"/mnt/{vol_name}"),
                })

        container["volumeMounts"] = volume_mounts

        # --- nodeSelector ---
        # center 节点: 不设 nodeSelector，走默认调度
        # edge 节点: 使用 KubeEdge 标准 label
        node_selector = {}
        if node_type == "edge":
            node_selector["node-role.kubernetes.io/edge"] = ""

        # --- tolerations ---
        tolerations = [
            {
                "key": "nvidia.com/gpu",
                "operator": "Exists",
                "effect": "NoSchedule",
            }
        ]
        if node_type == "edge":
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
                    "tolerationSeconds": 60,
                },
            ])

        # --- Pod Spec ---
        pod_spec = {
            "containers": [container],
            "volumes": volumes,
            "tolerations": tolerations,
            "restartPolicy": "Always",
        }
        # 只在有 nodeSelector 时设置（center 不需要）
        if node_selector:
            pod_spec["nodeSelector"] = node_selector
        # 如果指定了具体节点名，直接绑定（优先级最高，跳过 nodeSelector）
        if node_name:
            pod_spec.pop("nodeSelector", None)
            pod_spec["nodeName"] = node_name

        # --- Deployment ---
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": f"inst-{short_id}",
                "namespace": namespace or self.user_namespace(user_id),
                "labels": labels,
                "annotations": {
                    "lmaicloud/instance-name": instance_name,
                    "lmaicloud/node-type": node_type,
                    "lmaicloud/created-at": datetime.utcnow().isoformat(),
                },
            },
            "spec": {
                "replicas": instance_count,
                "selector": {
                    "matchLabels": {
                        "instance-id": instance_id,
                    }
                },
                "template": {
                    "metadata": {
                        "labels": labels,
                        "annotations": {
                            "lmaicloud/instance-name": instance_name,
                            "lmaicloud/node-type": node_type,
                        },
                    },
                    "spec": pod_spec,
                },
            },
        }
        return deployment

    def generate_deployment_yaml_file(self, deployment_spec: Dict, instance_id: str) -> str:
        """将 Deployment YAML 写入文件供审计，返回 YAML 字符串"""
        yaml_str = yaml.dump(deployment_spec, default_flow_style=False, allow_unicode=True)
        # 保存到文件
        yaml_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "deployment_yamls")
        os.makedirs(yaml_dir, exist_ok=True)
        file_path = os.path.join(yaml_dir, f"inst-{instance_id[:8]}.yaml")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(yaml_str)
        return yaml_str

    # ========== 实例生命周期 ==========

    def create_instance(
        self,
        instance_id: str,
        instance_name: str,
        user_id: str,
        image: str,
        gpu_count: int,
        cpu_cores: int,
        memory_gb: int,
        disk_gb: int,
        node_name: Optional[str] = None,
        node_type: str = "center",
        env_vars: Optional[List[Dict[str, str]]] = None,
        startup_command: Optional[str] = None,
        storage_mounts: Optional[List[Dict[str, Any]]] = None,
        instance_count: int = 1,
        pip_source: str = "default",
        conda_source: str = "default",
        apt_source: str = "default",
        namespace: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        创建 GPU 实例 (Deployment)

        Returns:
            {
                "success": bool,
                "deployment_name": str,
                "deployment_yaml": str,
                "internal_ip": str,
                "error": str (if failed)
            }
        """
        ns = namespace or self.user_namespace(user_id)
        self.k8s.ensure_namespace(ns)

        # 构建 Deployment YAML
        deployment_spec = self.build_deployment_yaml(
            instance_id=instance_id,
            instance_name=instance_name,
            user_id=user_id,
            image=image,
            gpu_count=gpu_count,
            cpu_cores=cpu_cores,
            memory_gb=memory_gb,
            disk_gb=disk_gb,
            node_name=node_name,
            node_type=node_type,
            env_vars=env_vars,
            startup_command=startup_command,
            storage_mounts=storage_mounts,
            instance_count=instance_count,
            pip_source=pip_source,
            conda_source=conda_source,
            apt_source=apt_source,
            namespace=ns,
        )

        # 保存 YAML 文件
        yaml_str = self.generate_deployment_yaml_file(deployment_spec, instance_id)

        # 创建 Deployment
        dep_name = self.k8s.create_deployment(ns, deployment_spec)
        if not dep_name:
            return {"success": False, "error": "Failed to create Deployment"}

        return {
            "success": True,
            "deployment_name": dep_name,
            "deployment_yaml": yaml_str,
            "namespace": ns,
            "internal_ip": "",
        }

    def start_instance(self, instance_id: str, namespace: str = "lmaicloud") -> bool:
        """启动实例 - 将 Deployment replicas 设为 1"""
        dep_name = f"inst-{instance_id[:8]}"
        return self.k8s.scale_deployment(dep_name, namespace, 1)

    def stop_instance(self, instance_id: str, namespace: str = "lmaicloud") -> bool:
        """停止实例 - 将 Deployment replicas 设为 0 (保留配置)"""
        dep_name = f"inst-{instance_id[:8]}"
        return self.k8s.scale_deployment(dep_name, namespace, 0)

    def release_instance(self, instance_id: str, namespace: str = "lmaicloud") -> bool:
        """删除实例 - 删除 Deployment（Pod 随 Deployment 级联删除）"""
        dep_name = f"inst-{instance_id[:8]}"
        self.k8s.delete_deployment(dep_name, namespace)
        self._cleanup_yaml_file(instance_id)
        return True

    def force_cleanup_instance(self, instance_id: str, namespace: str = "lmaicloud") -> bool:
        """
        强制清理实例所有 K8s 资源（不管当前状态）

        1. 删除 Deployment（级联删除正常 Pod）
        2. 强制删除所有关联 Pod（grace_period=0，处理 Terminating 卡住的情况）
        """
        dep_name = f"inst-{instance_id[:8]}"
        # 1. 删除 Deployment
        self.k8s.delete_deployment(dep_name, namespace)
        # 2. 强制删除所有关联 Pod（包括孤儿 Pod / Terminating Pod）
        try:
            pods = self.k8s.list_pods(
                namespace,
                label_selector=f"instance-id={instance_id}",
            )
            for pod in (pods or []):
                self.k8s.delete_pod(pod["name"], namespace, force=True)
        except Exception:
            pass  # 忽略 Pod 删除异常
        self._cleanup_yaml_file(instance_id)
        return True

    def _cleanup_yaml_file(self, instance_id: str):
        """清理实例对应的 Deployment YAML 审计文件"""
        try:
            yaml_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "deployment_yamls")
            file_path = os.path.join(yaml_dir, f"inst-{instance_id[:8]}.yaml")
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass

    def get_instance_status(self, instance_id: str, namespace: str = "lmaicloud") -> Optional[Dict[str, Any]]:
        """获取实例 Deployment 状态"""
        dep_name = f"inst-{instance_id[:8]}"
        dep = self.k8s.get_deployment(dep_name, namespace)
        if dep:
            return dep
        # fallback: 查找 Pod
        pod_name = f"instance-{instance_id[:8]}"
        return self.k8s.get_pod(pod_name, namespace)

    def get_instance_logs(self, instance_id: str, tail_lines: int = 100, namespace: str = "lmaicloud") -> Optional[str]:
        """获取实例日志 - 查找 Deployment 关联的 Pod"""
        try:
            pods = self.k8s.list_pods(
                namespace,
                label_selector=f"instance-id={instance_id}"
            )
            if pods:
                return self.k8s.get_pod_logs(pods[0]["name"], namespace, tail_lines)
            # fallback
            pod_name = f"instance-{instance_id[:8]}"
            return self.k8s.get_pod_logs(pod_name, namespace, tail_lines)
        except Exception as e:
            print(f"[PodManager] Error getting instance logs for {instance_id}: {e}")
            return None

    def get_deployment_yaml(self, instance_id: str) -> Optional[str]:
        """获取保存的 Deployment YAML 文件内容"""
        yaml_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "deployment_yamls")
        file_path = os.path.join(yaml_dir, f"inst-{instance_id[:8]}.yaml")
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                return f.read()
        return None


# 单例
_pod_manager: Optional[PodManager] = None


def get_pod_manager() -> PodManager:
    global _pod_manager
    if _pod_manager is None:
        _pod_manager = PodManager()
    return _pod_manager
