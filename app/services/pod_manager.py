"""
GPU Pod 管理服务

负责创建、管理GPU实例对应的K8s资源（Deployment + Service）
用户输入整合为 deployment.yaml，通过 K8s API 下发调度。
YAML 中通过 nodeSelector 区分边缘节点(edge) / 中心节点(center)。
"""
import json
import os
import random
import string
from datetime import datetime
from typing import Optional, Dict, Any, List

import yaml

from app.services.k8s_client import get_k8s_client


class PodManager:
    """GPU Pod 管理器 - 基于 Deployment"""

    NAMESPACE = "lmaicloud"
    SSH_PORT_RANGE = (30000, 32767)

    def __init__(self):
        self.k8s = get_k8s_client()

    # ========== 工具方法 ==========

    def generate_ssh_password(self, length: int = 12) -> str:
        chars = string.ascii_letters + string.digits
        return ''.join(random.choice(chars) for _ in range(length))

    def allocate_node_port(self) -> int:
        return random.randint(*self.SSH_PORT_RANGE)

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
        ssh_password: str,
        node_name: Optional[str] = None,
        node_type: str = "center",
        env_vars: Optional[List[Dict[str, str]]] = None,
        startup_command: Optional[str] = None,
        storage_mounts: Optional[List[Dict[str, Any]]] = None,
        instance_count: int = 1,
        pip_source: str = "default",
        conda_source: str = "default",
        apt_source: str = "default",
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
            {"name": "ROOT_PASSWORD", "value": ssh_password},
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
        # 判断是否为轻量镜像（无 sshd/chpasswd）
        lightweight_images = ("busybox", "alpine", "hello-world", "nginx", "httpd", "redis", "memcached")
        is_lightweight = any(image.lower().startswith(li) or f"/{li}" in image.lower() for li in lightweight_images)

        if is_lightweight:
            # 轻量镜像：跳过 SSH 初始化，直接用用户命令或保持前台运行
            if startup_command:
                full_cmd = startup_command
            else:
                full_cmd = "while true; do sleep 3600; done"
        else:
            # 完整镜像：初始化 SSH + 用户命令
            default_cmd = (
                'echo "root:${ROOT_PASSWORD}" | chpasswd && '
                "sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && "
                "sed -i 's/PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config && "
                "service ssh start"
            )
            if startup_command:
                full_cmd = f"{default_cmd} && {startup_command}"
            else:
                full_cmd = f"{default_cmd} && tail -f /dev/null"

        # --- 容器配置 ---
        container = {
            "name": "gpu-container",
            "image": image,
            "imagePullPolicy": "IfNotPresent",
            "resources": resources,
            "env": env_list,
            "ports": [
                {"containerPort": 22, "name": "ssh"},
                {"containerPort": 8888, "name": "jupyter"},
            ],
        }

        # 启动命令：统一用 command 字段传递完整命令，避免 args 为空导致 sh -c "" 退出
        if full_cmd:
            container["command"] = ["/bin/sh", "-c", full_cmd]

        # 完整镜像才加 SSH 探针，轻量镜像跳过（没有 sshd）
        if not is_lightweight:
            container["livenessProbe"] = {
                "tcpSocket": {"port": 22},
                "initialDelaySeconds": 30,
                "periodSeconds": 10,
            }
            container["readinessProbe"] = {
                "tcpSocket": {"port": 22},
                "initialDelaySeconds": 10,
                "periodSeconds": 5,
            }

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
                "namespace": self.NAMESPACE,
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

    def build_service_spec(
        self,
        instance_id: str,
        user_id: str,
        ssh_port: int,
        node_type: str = "center",
    ) -> Dict[str, Any]:
        """构建 Service 规格(暴露 SSH / Jupyter 端口)"""
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"svc-{instance_id[:8]}",
                "namespace": self.NAMESPACE,
                "labels": {
                    "instance-id": instance_id,
                    "user-id": user_id,
                    "node-type": node_type,
                },
            },
            "spec": {
                "type": "NodePort",
                "selector": {
                    "instance-id": instance_id,
                },
                "ports": [
                    {
                        "name": "ssh",
                        "port": 22,
                        "targetPort": 22,
                        "nodePort": ssh_port,
                    },
                    {
                        "name": "jupyter",
                        "port": 8888,
                        "targetPort": 8888,
                    },
                ],
            },
        }

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
    ) -> Dict[str, Any]:
        """
        创建 GPU 实例 (Deployment + Service)

        Returns:
            {
                "success": bool,
                "ssh_host": str,
                "ssh_port": int,
                "ssh_password": str,
                "deployment_name": str,
                "deployment_yaml": str,
                "internal_ip": str,
                "error": str (if failed)
            }
        """
        self.k8s.ensure_namespace(self.NAMESPACE)

        ssh_password = self.generate_ssh_password()
        ssh_port = self.allocate_node_port()

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
            ssh_password=ssh_password,
            node_name=node_name,
            node_type=node_type,
            env_vars=env_vars,
            startup_command=startup_command,
            storage_mounts=storage_mounts,
            instance_count=instance_count,
            pip_source=pip_source,
            conda_source=conda_source,
            apt_source=apt_source,
        )

        # 保存 YAML 文件
        yaml_str = self.generate_deployment_yaml_file(deployment_spec, instance_id)

        # 创建 Deployment
        dep_name = self.k8s.create_deployment(self.NAMESPACE, deployment_spec)
        if not dep_name:
            return {"success": False, "error": "Failed to create Deployment"}

        # 创建 Service
        service_spec = self.build_service_spec(instance_id, user_id, ssh_port, node_type)
        svc_name = self.k8s.create_service(self.NAMESPACE, service_spec)
        if not svc_name:
            self.k8s.delete_deployment(dep_name, self.NAMESPACE)
            return {"success": False, "error": "Failed to create Service"}

        # 获取节点 IP
        ssh_host = ""
        if node_name:
            node_info = self.k8s.get_node(node_name)
            if node_info:
                ssh_host = node_info.get("ip", "")

        return {
            "success": True,
            "ssh_host": ssh_host,
            "ssh_port": ssh_port,
            "ssh_password": ssh_password,
            "deployment_name": dep_name,
            "deployment_yaml": yaml_str,
            "internal_ip": "",
        }

    def start_instance(self, instance_id: str) -> bool:
        """启动实例 - 将 Deployment replicas 设为 1"""
        dep_name = f"inst-{instance_id[:8]}"
        return self.k8s.scale_deployment(dep_name, self.NAMESPACE, 1)

    def stop_instance(self, instance_id: str) -> bool:
        """停止实例 - 将 Deployment replicas 设为 0 (保留配置)"""
        dep_name = f"inst-{instance_id[:8]}"
        return self.k8s.scale_deployment(dep_name, self.NAMESPACE, 0)

    def release_instance(self, instance_id: str) -> bool:
        """删除实例 - 删除 Deployment + Service（Pod 随 Deployment 级联删除）"""
        dep_name = f"inst-{instance_id[:8]}"
        svc_name = f"svc-{instance_id[:8]}"
        self.k8s.delete_deployment(dep_name, self.NAMESPACE)
        self.k8s.delete_service(svc_name, self.NAMESPACE)
        return True

    def force_cleanup_instance(self, instance_id: str) -> bool:
        """
        强制清理实例所有 K8s 资源（不管当前状态）

        1. 删除 Deployment（级联删除正常 Pod）
        2. 强制删除所有关联 Pod（grace_period=0，处理 Terminating 卡住的情况）
        3. 删除 Service
        """
        dep_name = f"inst-{instance_id[:8]}"
        svc_name = f"svc-{instance_id[:8]}"
        # 1. 删除 Deployment
        self.k8s.delete_deployment(dep_name, self.NAMESPACE)
        # 2. 强制删除所有关联 Pod（包括孤儿 Pod / Terminating Pod）
        try:
            pods = self.k8s.list_pods(
                self.NAMESPACE,
                label_selector=f"instance-id={instance_id}",
            )
            for pod in (pods or []):
                self.k8s.delete_pod(pod["name"], self.NAMESPACE, force=True)
        except Exception:
            pass  # 忽略 Pod 删除异常
        # 3. 删除 Service
        self.k8s.delete_service(svc_name, self.NAMESPACE)
        return True

    def get_instance_status(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """获取实例 Deployment 状态"""
        dep_name = f"inst-{instance_id[:8]}"
        dep = self.k8s.get_deployment(dep_name, self.NAMESPACE)
        if dep:
            return dep
        # fallback: 查找 Pod
        pod_name = f"instance-{instance_id[:8]}"
        return self.k8s.get_pod(pod_name, self.NAMESPACE)

    def get_instance_logs(self, instance_id: str, tail_lines: int = 100) -> Optional[str]:
        """获取实例日志 - 查找 Deployment 关联的 Pod"""
        try:
            pods = self.k8s.list_pods(
                self.NAMESPACE,
                label_selector=f"instance-id={instance_id}"
            )
            if pods:
                return self.k8s.get_pod_logs(pods[0]["name"], self.NAMESPACE, tail_lines)
            # fallback
            pod_name = f"instance-{instance_id[:8]}"
            return self.k8s.get_pod_logs(pod_name, self.NAMESPACE, tail_lines)
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
