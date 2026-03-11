"""
资源调度服务

负责 GPU 实例的创建、调度、生命周期管理
"""
from typing import Optional, List, Dict, Any
from uuid import UUID

from app.services.k8s_client import get_k8s_client


class InstanceScheduler:
    """GPU 实例调度器"""
    
    NAMESPACE = "lmaicloud-instances"
    
    def __init__(self):
        self.k8s = get_k8s_client()
        self.k8s.ensure_namespace(self.NAMESPACE)
    
    def create_instance(
        self,
        instance_id: UUID,
        user_id: UUID,
        node_name: str,
        gpu_count: int,
        image: str,
        cpu_cores: int = 8,
        memory_gb: int = 32,
        disk_gb: int = 50,
        env_vars: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """创建 GPU 实例"""
        pod_name = f"inst-{str(instance_id)[:8]}"
        ssh_port = 30000 + (hash(str(instance_id)) % 10000)
        
        labels = {
            "app": "lmaicloud-instance",
            "instance-id": str(instance_id),
            "user-id": str(user_id),
        }
        
        # Pod 定义
        pod_spec = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": self.NAMESPACE, "labels": labels},
            "spec": {
                "restartPolicy": "Never",
                "nodeName": node_name,
                "containers": [{
                    "name": "gpu-container",
                    "image": image,
                    "resources": {
                        "requests": {"cpu": str(cpu_cores), "memory": f"{memory_gb}Gi", "nvidia.com/gpu": str(gpu_count)},
                        "limits": {"cpu": str(cpu_cores), "memory": f"{memory_gb}Gi", "nvidia.com/gpu": str(gpu_count)},
                    },
                    "env": [{"name": k, "value": v} for k, v in (env_vars or {}).items()],
                    "ports": [{"containerPort": 22, "name": "ssh"}, {"containerPort": 8888, "name": "jupyter"}],
                    "volumeMounts": [{"name": "data", "mountPath": "/root/data"}],
                }],
                "volumes": [{"name": "data", "emptyDir": {"sizeLimit": f"{disk_gb}Gi"}}],
            },
        }
        
        # 创建 Pod
        result = self.k8s.create_pod(self.NAMESPACE, pod_spec)
        if not result:
            return {"success": False, "error": "Failed to create pod"}
        
        # 创建 NodePort Service
        svc_spec = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": f"svc-{str(instance_id)[:8]}", "namespace": self.NAMESPACE, "labels": labels},
            "spec": {
                "type": "NodePort",
                "selector": labels,
                "ports": [{"name": "ssh", "port": 22, "targetPort": 22, "nodePort": ssh_port}],
            },
        }
        self.k8s.create_service(self.NAMESPACE, svc_spec)
        
        return {"success": True, "pod_name": pod_name, "ssh_port": ssh_port, "status": "Creating"}
    
    def stop_instance(self, instance_id: UUID) -> bool:
        """停止实例"""
        pod_name = f"inst-{str(instance_id)[:8]}"
        return self.k8s.delete_pod(pod_name, self.NAMESPACE)
    
    def release_instance(self, instance_id: UUID) -> bool:
        """释放实例（删除所有资源）"""
        prefix = str(instance_id)[:8]
        self.k8s.delete_service(f"svc-{prefix}", self.NAMESPACE)
        return self.k8s.delete_pod(f"inst-{prefix}", self.NAMESPACE)
    
    def get_instance_status(self, instance_id: UUID) -> Optional[Dict[str, Any]]:
        """获取实例状态"""
        pod = self.k8s.get_pod(f"inst-{str(instance_id)[:8]}", self.NAMESPACE)
        if not pod:
            return None
        return {
            "instance_id": str(instance_id),
            "status": pod["status"],
            "pod_ip": pod["pod_ip"],
            "host_ip": pod["host_ip"],
            "node_name": pod["node_name"],
        }
    
    def get_instance_logs(self, instance_id: UUID, tail_lines: int = 100) -> Optional[str]:
        """获取实例日志"""
        return self.k8s.get_pod_logs(f"inst-{str(instance_id)[:8]}", self.NAMESPACE, tail_lines)


class NodeManager:
    """节点管理器"""
    
    def __init__(self):
        self.k8s = get_k8s_client()
    
    def get_available_nodes(self, gpu_model: Optional[str] = None, min_gpu: int = 1) -> List[Dict[str, Any]]:
        """获取可用节点列表"""
        label_selector = f"gpu-model={gpu_model}" if gpu_model else None
        nodes = self.k8s.list_nodes(label_selector)
        
        return [
            n for n in nodes
            if n["status"] == "Ready" and not n["unschedulable"] and n["gpu_allocatable"] >= min_gpu
        ]
    
    def get_node_details(self, node_name: str) -> Optional[Dict[str, Any]]:
        """获取节点详情"""
        node = self.k8s.get_node(node_name)
        if not node:
            return None
        
        metrics = self.k8s.get_node_metrics(node_name)
        pods = self.k8s.list_pods(label_selector=f"spec.nodeName={node_name}")
        
        return {**node, "metrics": metrics, "pod_count": len(pods)}
    
    def set_maintenance(self, node_name: str, enable: bool) -> bool:
        """设置维护模式"""
        return self.k8s.cordon_node(node_name) if enable else self.k8s.uncordon_node(node_name)


# ========== 单例 ==========

_instance_scheduler: Optional[InstanceScheduler] = None
_node_manager: Optional[NodeManager] = None


def get_instance_scheduler() -> InstanceScheduler:
    global _instance_scheduler
    if _instance_scheduler is None:
        _instance_scheduler = InstanceScheduler()
    return _instance_scheduler


def get_node_manager() -> NodeManager:
    global _node_manager
    if _node_manager is None:
        _node_manager = NodeManager()
    return _node_manager
