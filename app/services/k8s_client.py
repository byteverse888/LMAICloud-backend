"""
Kubernetes 客户端服务

一期架构: 单集群 K8s 管理
后续扩展: 多集群时再对接 Karmada
"""
import os
import time
from typing import Optional, List, Dict, Any
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

from app.config import settings


class K8sClient:
    """Kubernetes 客户端封装"""
    
    def __init__(self, kubeconfig_path: Optional[str] = None, context: Optional[str] = None):
        """
        初始化 K8s 客户端
        
        Args:
            kubeconfig_path: kubeconfig 文件路径
            context: context 名称
        """
        self._initialized = False
        try:
            if kubeconfig_path:
                config.load_kube_config(config_file=kubeconfig_path, context=context)
            elif os.path.exists(os.path.expanduser("~/.kube/config")):
                config.load_kube_config(context=context)
            else:
                config.load_incluster_config()
            self._initialized = True
        except Exception as e:
            print(f"[K8s] Warning: Failed to load kubeconfig: {e}")
        
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.batch_v1 = client.BatchV1Api()
        self.custom_objects = client.CustomObjectsApi()
    
    @property
    def is_connected(self) -> bool:
        return self._initialized
    
    # ========== 节点管理 ==========
    
    def list_nodes(self, label_selector: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取节点列表"""
        if not self._initialized:
            return []
        try:
            nodes = self.core_v1.list_node(label_selector=label_selector)
            return [self._parse_node(node) for node in nodes.items]
        except ApiException as e:
            print(f"[K8s] Error listing nodes: {e}")
            return []
    
    def get_node(self, name: str) -> Optional[Dict[str, Any]]:
        """获取节点详情"""
        if not self._initialized:
            return None
        try:
            node = self.core_v1.read_node(name)
            return self._parse_node(node)
        except ApiException as e:
            print(f"[K8s] Error getting node {name}: {e}")
            return None
    
    def get_node_metrics(self, name: str) -> Optional[Dict[str, Any]]:
        """获取节点资源指标"""
        if not self._initialized:
            return None
        try:
            metrics = self.custom_objects.get_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="nodes",
                name=name
            )
            return {
                "cpu_usage": metrics.get("usage", {}).get("cpu"),
                "memory_usage": metrics.get("usage", {}).get("memory"),
            }
        except ApiException:
            return None
    
    def cordon_node(self, name: str) -> bool:
        """设置节点不可调度"""
        if not self._initialized:
            return False
        try:
            self.core_v1.patch_node(name, {"spec": {"unschedulable": True}})
            return True
        except ApiException as e:
            print(f"[K8s] Error cordoning node {name}: {e}")
            return False
    
    def uncordon_node(self, name: str) -> bool:
        """取消节点不可调度"""
        if not self._initialized:
            return False
        try:
            self.core_v1.patch_node(name, {"spec": {"unschedulable": False}})
            return True
        except ApiException as e:
            print(f"[K8s] Error uncordoning node {name}: {e}")
            return False
    
    def delete_node(self, name: str) -> bool:
        """删除节点"""
        if not self._initialized:
            return False
        try:
            self.core_v1.delete_node(name)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting node {name}: {e}")
            return False
    
    def _parse_node(self, node) -> Dict[str, Any]:
        """解析 Node 对象"""
        status = node.status
        conditions = {c.type: c.status for c in status.conditions} if status.conditions else {}
        capacity = status.capacity or {}
        allocatable = status.allocatable or {}
        
        # GPU 信息
        gpu_count = int(capacity.get("nvidia.com/gpu", 0))
        gpu_allocatable = int(allocatable.get("nvidia.com/gpu", 0))
        
        return {
            "name": node.metadata.name,
            "labels": node.metadata.labels or {},
            "status": "Ready" if conditions.get("Ready") == "True" else "NotReady",
            "unschedulable": node.spec.unschedulable or False,
            "cpu_capacity": capacity.get("cpu"),
            "memory_capacity": capacity.get("memory"),
            "gpu_count": gpu_count,
            "gpu_allocatable": gpu_allocatable,
            "ip": next((a.address for a in status.addresses if a.type == "InternalIP"), None),
            "os": status.node_info.os_image if status.node_info else None,
            "kubelet_version": status.node_info.kubelet_version if status.node_info else None,
            "conditions": conditions,
            "created_at": node.metadata.creation_timestamp.isoformat() if node.metadata.creation_timestamp else None,
        }
    
    # ========== Pod 管理 ==========
    
    def list_pods(self, namespace: str = "default", label_selector: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取 Pod 列表"""
        if not self._initialized:
            return []
        try:
            pods = self.core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
            return [self._parse_pod(pod) for pod in pods.items]
        except ApiException as e:
            print(f"[K8s] Error listing pods: {e}")
            return []
    
    def get_pod(self, name: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """获取 Pod 详情"""
        if not self._initialized:
            return None
        try:
            pod = self.core_v1.read_namespaced_pod(name, namespace)
            return self._parse_pod(pod)
        except ApiException:
            return None
    
    def create_pod(self, namespace: str, pod_spec: Dict) -> Optional[str]:
        """创建 Pod"""
        if not self._initialized:
            return None
        try:
            result = self.core_v1.create_namespaced_pod(namespace, pod_spec)
            return result.metadata.name
        except ApiException as e:
            print(f"[K8s] Error creating pod: {e}")
            return None
    
    def delete_pod(self, name: str, namespace: str = "default") -> bool:
        """删除 Pod"""
        if not self._initialized:
            return False
        try:
            self.core_v1.delete_namespaced_pod(name, namespace)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting pod {name}: {e}")
            return False
    
    def get_pod_logs(self, name: str, namespace: str = "default", tail_lines: int = 100) -> Optional[str]:
        """获取 Pod 日志"""
        if not self._initialized:
            return None
        try:
            return self.core_v1.read_namespaced_pod_log(name, namespace, tail_lines=tail_lines)
        except ApiException:
            return None

    def exec_in_pod(self, name: str, namespace: str, command: List[str], container: str = None) -> Optional[str]:
        """在 Pod 中执行命令（等价于 kubectl exec）"""
        if not self._initialized:
            return None
        try:
            kwargs = {
                "name": name,
                "namespace": namespace,
                "command": command,
                "stderr": True,
                "stdin": False,
                "stdout": True,
                "tty": False,
            }
            if container:
                kwargs["container"] = container
            resp = k8s_stream(self.core_v1.connect_get_namespaced_pod_exec, **kwargs)
            return resp
        except ApiException as e:
            print(f"[K8s] Error exec in pod {name}: {e}")
            return None

    def exec_interactive_stream(self, name: str, namespace: str, command: List[str], container: str = None):
        """打开 Pod 交互式 exec 流（用于 WebShell）"""
        if not self._initialized:
            return None
        try:
            kwargs = {
                "name": name,
                "namespace": namespace,
                "command": command,
                "stderr": True,
                "stdin": True,
                "stdout": True,
                "tty": True,
                "_preload_content": False,
            }
            if container:
                kwargs["container"] = container
            return k8s_stream(self.core_v1.connect_get_namespaced_pod_exec, **kwargs)
        except ApiException as e:
            print(f"[K8s] Error exec stream in pod {name}: {e}")
            return None
    
    def _parse_pod(self, pod) -> Dict[str, Any]:
        """解析 Pod 对象"""
        status = pod.status
        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "labels": pod.metadata.labels or {},
            "status": status.phase,
            "pod_ip": status.pod_ip,
            "host_ip": status.host_ip,
            "node_name": pod.spec.node_name,
            "containers": [{"name": c.name, "image": c.image} for c in pod.spec.containers],
            "created_at": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
        }
    
    # ========== Service 管理 ==========
    
    def create_service(self, namespace: str, service_spec: Dict) -> Optional[str]:
        """创建 Service"""
        if not self._initialized:
            return None
        try:
            result = self.core_v1.create_namespaced_service(namespace, service_spec)
            return result.metadata.name
        except ApiException as e:
            print(f"[K8s] Error creating service: {e}")
            return None
    
    def delete_service(self, name: str, namespace: str = "default") -> bool:
        """删除 Service"""
        if not self._initialized:
            return False
        try:
            self.core_v1.delete_namespaced_service(name, namespace)
            return True
        except ApiException:
            return False
    
    # ========== Namespace 管理 ==========
    
    def ensure_namespace(self, name: str) -> bool:
        """确保命名空间存在"""
        if not self._initialized:
            return False
        try:
            self.core_v1.read_namespace(name)
            return True
        except ApiException:
            try:
                self.core_v1.create_namespace({"metadata": {"name": name}})
                return True
            except ApiException as e:
                print(f"[K8s] Error creating namespace {name}: {e}")
                return False

    # ========== Deployment 管理 ==========

    def create_deployment(self, namespace: str, deployment_spec: Dict) -> Optional[str]:
        """创建 Deployment"""
        if not self._initialized:
            return None
        try:
            result = self.apps_v1.create_namespaced_deployment(namespace, deployment_spec)
            return result.metadata.name
        except ApiException as e:
            print(f"[K8s] Error creating deployment: {e}")
            return None

    def get_deployment(self, name: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """获取 Deployment 详情"""
        if not self._initialized:
            return None
        try:
            dep = self.apps_v1.read_namespaced_deployment(name, namespace)
            return {
                "name": dep.metadata.name,
                "namespace": dep.metadata.namespace,
                "replicas": dep.spec.replicas,
                "ready_replicas": dep.status.ready_replicas or 0,
                "available_replicas": dep.status.available_replicas or 0,
                "labels": dep.metadata.labels or {},
                "created_at": dep.metadata.creation_timestamp.isoformat() if dep.metadata.creation_timestamp else None,
            }
        except ApiException as e:
            print(f"[K8s] Error getting deployment {name}: {e}")
            return None

    def delete_deployment(self, name: str, namespace: str = "default") -> bool:
        """删除 Deployment"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.delete_namespaced_deployment(name, namespace)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting deployment {name}: {e}")
            return False

    def scale_deployment(self, name: str, namespace: str, replicas: int) -> bool:
        """调整 Deployment 副本数"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.patch_namespaced_deployment_scale(
                name, namespace, {"spec": {"replicas": replicas}}
            )
            return True
        except ApiException as e:
            print(f"[K8s] Error scaling deployment {name}: {e}")
            return False

    def wait_for_pod_ready(
        self, namespace: str, label_selector: str,
        timeout: int = 120, interval: int = 3
    ) -> Dict[str, Any]:
        """
        轮询等待至少一个 Pod 进入 Running 状态

        Returns:
            {"ready": bool, "pod_name": str, "pod_ip": str, "host_ip": str, "message": str}
        """
        if not self._initialized:
            return {"ready": False, "message": "K8s client not initialized"}

        deadline = time.time() + timeout
        last_status = ""
        while time.time() < deadline:
            try:
                pods = self.core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
                for pod in pods.items:
                    phase = pod.status.phase
                    last_status = phase
                    if phase == "Running":
                        # 检查容器是否全部 Ready
                        container_statuses = pod.status.container_statuses
                        if container_statuses:
                            all_ready = all(cs.ready for cs in container_statuses)
                        else:
                            # 容器状态尚未上报，视为未就绪
                            all_ready = False
                        if all_ready:
                            return {
                                "ready": True,
                                "pod_name": pod.metadata.name,
                                "pod_ip": pod.status.pod_ip or "",
                                "host_ip": pod.status.host_ip or "",
                                "message": "Pod is running and ready",
                            }
            except ApiException as e:
                print(f"[K8s] Error polling pods: {e}")
            time.sleep(interval)

        return {
            "ready": False,
            "pod_name": "",
            "pod_ip": "",
            "host_ip": "",
            "message": f"Timeout after {timeout}s, last phase: {last_status}",
        }


# ========== 单例 ==========

_k8s_client: Optional[K8sClient] = None


def get_k8s_client() -> K8sClient:
    """获取 K8s 客户端单例"""
    global _k8s_client
    if _k8s_client is None:
        kubeconfig_path = settings.kubeconfig_path if settings.kubeconfig_path else None
        _k8s_client = K8sClient(kubeconfig_path=kubeconfig_path)
    return _k8s_client
