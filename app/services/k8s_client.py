"""
Kubernetes 客户端服务

一期架构: 单集群 K8s 管理
后续扩展: 多集群时再对接 Karmada
"""
import os
import time
import functools
import logging
from typing import Optional, List, Dict, Any
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream

from app.config import settings

logger = logging.getLogger("lmaicloud.k8s")


def _k8s_retry(max_retries: int = 2, delay: float = 1.0):
    """K8s API 调用重试装饰器，遇到连接级错误时重建客户端并重试"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            # 断路器检查：冷却期内直接拒绝调用
            if self._circuit_open:
                elapsed = time.time() - self._last_failure_time
                if elapsed < self._circuit_cooldown:
                    raise ConnectionError(
                        f"K8s circuit breaker OPEN, retry in {int(self._circuit_cooldown - elapsed)}s"
                    )
                # 冷却期已过，尝试半开状态
                logger.info("[K8s] Circuit breaker half-open, probing...")

            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    result = func(self, *args, **kwargs)
                    # 调用成功，重置断路器
                    if self._circuit_open:
                        logger.info("[K8s] Connection restored, circuit breaker CLOSED")
                        self._circuit_open = False
                        self._consecutive_failures = 0
                    return result
                except ApiException as e:
                    if e.status == 0 and attempt < max_retries:
                        last_error = e
                        logger.debug(f"[K8s] {func.__name__} attempt {attempt+1} connection error, rebuilding...")
                        self._rebuild_client()
                        time.sleep(delay)
                        continue
                    raise
                except Exception as e:
                    last_error = e
                    err_msg = str(e)
                    if attempt < max_retries and ("Handshake" in err_msg or "Connection" in err_msg or "Timeout" in err_msg):
                        logger.debug(f"[K8s] {func.__name__} attempt {attempt+1} failed, rebuilding...")
                        self._rebuild_client()
                        time.sleep(delay)
                    else:
                        raise
            # 所有重试均失败，累加断路器计数
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._circuit_threshold:
                self._circuit_open = True
                self._last_failure_time = time.time()
                logger.warning(
                    f"[K8s] Circuit breaker OPEN after {self._consecutive_failures} consecutive failures, "
                    f"cooldown {self._circuit_cooldown}s"
                )
            raise last_error  # type: ignore
        return wrapper
    return decorator


class K8sClient:
    """Kubernetes 客户端封装"""
    
    def __init__(self, kubeconfig_path: Optional[str] = None, context: Optional[str] = None):
        """
        初始化 K8s 客户端
        
        Args:
            kubeconfig_path: kubeconfig 文件路径
            context: context 名称
        """
        self._kubeconfig_path = kubeconfig_path
        self._context = context
        self._initialized = False
        self._api_client = None
        # 断路器状态
        self._circuit_open = False
        self._consecutive_failures = 0
        self._circuit_threshold = 3       # 连续失败3次后断开
        self._circuit_cooldown = 120      # 冷却期120秒
        self._last_failure_time = 0.0
        self._load_config()
        self._create_api_objects()

    def _load_config(self):
        """加载 kubeconfig"""
        try:
            if self._kubeconfig_path:
                config.load_kube_config(config_file=self._kubeconfig_path, context=self._context)
            elif os.path.exists(os.path.expanduser("~/.kube/config")):
                config.load_kube_config(context=self._context)
            else:
                config.load_incluster_config()
            self._initialized = True
        except Exception as e:
            print(f"[K8s] Warning: Failed to load kubeconfig: {e}")

    def _create_api_objects(self):
        """创建 API 对象，使用显式 ApiClient 以隔离连接池"""
        # 关闭旧连接池
        if self._api_client:
            try:
                self._api_client.close()
            except Exception:
                pass
        # 创建新的 ApiClient（独立连接池）
        self._api_client = client.ApiClient()
        self.core_v1 = client.CoreV1Api(self._api_client)
        self.apps_v1 = client.AppsV1Api(self._api_client)
        self.batch_v1 = client.BatchV1Api(self._api_client)
        self.custom_objects = client.CustomObjectsApi(self._api_client)

    def _rebuild_client(self):
        """重建 K8s API 客户端（连接级错误后调用）"""
        logger.debug("[K8s] Rebuilding API client with fresh connection pool...")
        self._load_config()
        self._create_api_objects()

    @property
    def circuit_open(self) -> bool:
        """断路器是否开启（K8s 不可达）"""
        if not self._circuit_open:
            return False
        elapsed = time.time() - self._last_failure_time
        return elapsed < self._circuit_cooldown
    
    @property
    def is_connected(self) -> bool:
        return self._initialized
    
    # ========== 节点管理 ==========
    
    @_k8s_retry(max_retries=2, delay=1.0)
    def list_nodes(self, label_selector: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取节点列表"""
        if not self._initialized:
            return []
        try:
            nodes = self.core_v1.list_node(label_selector=label_selector)
            return [self._parse_node(node) for node in nodes.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing nodes: {e}")
            return []
        except Exception as e:
            print(f"[K8s] Error listing nodes: {e}")
            return []
    
    @_k8s_retry(max_retries=1, delay=0.5)
    def get_node(self, name: str) -> Optional[Dict[str, Any]]:
        """获取节点详情"""
        if not self._initialized:
            return None
        try:
            node = self.core_v1.read_node(name)
            return self._parse_node(node)
        except ApiException as e:
            if e.status == 0:
                raise
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
        
        # GPU 信息：优先从 capacity 读取，边缘节点可能没有 Device Plugin，回退读标签
        labels = node.metadata.labels or {}
        gpu_count = int(capacity.get("nvidia.com/gpu", 0))
        gpu_allocatable = int(allocatable.get("nvidia.com/gpu", 0))
        # 边缘节点 fallback：从 nvidia.com/gpu.count 标签读取
        if gpu_count == 0:
            gpu_count = int(labels.get("nvidia.com/gpu.count", 0) or 0)
        if gpu_allocatable == 0 and gpu_count > 0:
            gpu_allocatable = gpu_count
        
        return {
            "name": node.metadata.name,
            "labels": labels,
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
    
    @_k8s_retry(max_retries=2, delay=1.0)
    def list_pods(self, namespace: str = "default", label_selector: Optional[str] = None,
                  all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 Pod 列表"""
        if not self._initialized:
            return []
        kwargs = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if all_namespaces:
            pods = self.core_v1.list_pod_for_all_namespaces(**kwargs)
        else:
            pods = self.core_v1.list_namespaced_pod(namespace, **kwargs)
        return [self._parse_pod(pod) for pod in pods.items]
    
    @_k8s_retry(max_retries=1, delay=0.5)
    def get_pod(self, name: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """获取 Pod 详情"""
        if not self._initialized:
            return None
        try:
            pod = self.core_v1.read_namespaced_pod(name, namespace)
            return self._parse_pod(pod)
        except ApiException as e:
            if e.status == 0:
                raise
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
    
    def delete_pod(self, name: str, namespace: str = "default", force: bool = False) -> bool:
        """删除 Pod。force=True 时使用 grace_period_seconds=0 强制立即删除（适用于 Terminating 卡住的 Pod）"""
        if not self._initialized:
            return False
        try:
            if force:
                from kubernetes.client import V1DeleteOptions
                body = V1DeleteOptions(grace_period_seconds=0, propagation_policy="Background")
                self.core_v1.delete_namespaced_pod(name, namespace, body=body)
            else:
                self.core_v1.delete_namespaced_pod(name, namespace)
            return True
        except ApiException as e:
            if e.status == 404:
                # Pod 已不存在，视为删除成功
                return True
            print(f"[K8s] Error deleting pod {name}: {e}")
            return False
    
    @_k8s_retry(max_retries=2, delay=1.0)
    def get_pod_logs(self, name: str, namespace: str = "default",
                     tail_lines: int = 100, container: Optional[str] = None) -> Optional[str]:
        """获取 Pod 日志"""
        if not self._initialized:
            print("[K8s] get_pod_logs: client not initialized")
            return None
        try:
            kwargs = {"name": name, "namespace": namespace, "tail_lines": tail_lines}
            if container:
                kwargs["container"] = container
            return self.core_v1.read_namespaced_pod_log(**kwargs)
        except ApiException as e:
            if e.status == 0:
                raise  # 连接级错误交给 _k8s_retry 重试
            print(f"[K8s] Error getting pod logs {namespace}/{name}: {e.status} {e.reason}")
            return None

    def _create_stream_core_v1(self):
        """创建独立的 CoreV1Api 用于 exec/stream 操作，避免污染共享 api_client"""
        stream_api_client = client.ApiClient()
        return client.CoreV1Api(stream_api_client)

    @_k8s_retry(max_retries=1, delay=0.5)
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
            stream_core = self._create_stream_core_v1()
            resp = k8s_stream(stream_core.connect_get_namespaced_pod_exec, **kwargs)
            return resp
        except ApiException as e:
            if e.status == 0:
                raise  # 连接级错误交给 _k8s_retry 重试
            print(f"[K8s] Error exec in pod {name}: {e}")
            return None

    @_k8s_retry(max_retries=1, delay=0.5)
    def exec_interactive_stream(self, name: str, namespace: str, command: List[str], container: str = None, tty: bool = True):
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
                "tty": tty,
                "_preload_content": False,
            }
            if container:
                kwargs["container"] = container
            stream_core = self._create_stream_core_v1()
            return k8s_stream(stream_core.connect_get_namespaced_pod_exec, **kwargs)
        except ApiException as e:
            if e.status == 0:
                raise  # 连接级错误交给 _k8s_retry 重试
            print(f"[K8s] Error exec stream in pod {name}: {e}")
            return None
    
    def _parse_pod(self, pod) -> Dict[str, Any]:
        """解析 Pod 对象"""
        status = pod.status
        # 容器状态详情
        container_statuses = []
        restart_count = 0
        if status.container_statuses:
            for cs in status.container_statuses:
                restart_count += cs.restart_count or 0
                state = "waiting"
                reason = ""
                if cs.state:
                    if cs.state.running:
                        state = "running"
                    elif cs.state.terminated:
                        state = "terminated"
                        reason = cs.state.terminated.reason or ""
                    elif cs.state.waiting:
                        state = "waiting"
                        reason = cs.state.waiting.reason or ""
                container_statuses.append({
                    "name": cs.name,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count or 0,
                    "state": state,
                    "reason": reason,
                    "image": cs.image,
                })
        annotations = pod.metadata.annotations or {}
        # 容器就绪统计
        total_containers = len(pod.spec.containers) if pod.spec.containers else 0
        ready_containers = sum(1 for cs in (status.container_statuses or []) if cs.ready)
        # 细化 status：Terminating（删除中）→ 优先判断；Running 但容器未全就绪 → starting
        phase = status.phase or "Unknown"
        is_terminating = pod.metadata.deletion_timestamp is not None
        if is_terminating:
            effective_status = "Terminating"
        elif phase == "Running" and ready_containers < total_containers:
            effective_status = "starting"
        else:
            effective_status = phase
        pod_labels = pod.metadata.labels or {}
        return {
            "name": pod.metadata.name,
            "namespace": pod.metadata.namespace,
            "labels": pod_labels,
            "annotations": annotations,
            "instance_name": annotations.get("lmaicloud/instance-name", ""),
            "instance_id": pod_labels.get("instance-id", ""),
            "openclaw_instance_id": pod_labels.get("openclaw-instance", ""),
            "status": phase,                    # 原始 K8s Phase（供过滤/筛选用）
            "effective_status": effective_status,  # 业务状态（考虑容器就绪和 Terminating）
            "is_terminating": is_terminating,
            "pod_ip": status.pod_ip,
            "host_ip": status.host_ip,
            "node_name": pod.spec.node_name,
            "restart_count": restart_count,
            "ready_containers": ready_containers,
            "total_containers": total_containers,
            "containers": [{"name": c.name, "image": c.image} for c in pod.spec.containers],
            "container_statuses": container_statuses,
            "created_at": pod.metadata.creation_timestamp.isoformat() if pod.metadata.creation_timestamp else None,
        }

    def get_pod_events(self, name: str, namespace: str = "default") -> List[Dict[str, Any]]:
        """获取 Pod 相关事件"""
        if not self._initialized:
            return []
        try:
            field_selector = f"involvedObject.name={name},involvedObject.namespace={namespace}"
            events = self.core_v1.list_namespaced_event(namespace, field_selector=field_selector)
            return [{
                "type": e.type,
                "reason": e.reason,
                "message": e.message,
                "count": e.count,
                "first_timestamp": e.first_timestamp.isoformat() if e.first_timestamp else None,
                "last_timestamp": e.last_timestamp.isoformat() if e.last_timestamp else None,
            } for e in events.items]
        except ApiException:
            return []
    
    # ========== Service 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_services(self, namespace: str = "default", label_selector: Optional[str] = None,
                      all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 Service 列表"""
        if not self._initialized:
            return []
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if all_namespaces:
                svcs = self.core_v1.list_service_for_all_namespaces(**kwargs)
            else:
                svcs = self.core_v1.list_namespaced_service(namespace, **kwargs)
            return [self._parse_service(s) for s in svcs.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing services: {e}")
            return []

    def get_service(self, name: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """获取 Service 详情"""
        if not self._initialized:
            return None
        try:
            svc = self.core_v1.read_namespaced_service(name, namespace)
            return self._parse_service(svc)
        except ApiException as e:
            print(f"[K8s] Error getting service {name}: {e}")
            return None

    @_k8s_retry(max_retries=1, delay=0.5)
    def create_service(self, namespace: str, service_spec: Dict) -> Optional[str]:
        """创建 Service"""
        if not self._initialized:
            return None
        try:
            result = self.core_v1.create_namespaced_service(namespace, service_spec)
            return result.metadata.name
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error creating service: {e}")
            return None

    def update_service(self, name: str, namespace: str, spec: Dict) -> bool:
        """更新 Service"""
        if not self._initialized:
            return False
        try:
            self.core_v1.patch_namespaced_service(name, namespace, spec)
            return True
        except ApiException as e:
            print(f"[K8s] Error updating service {name}: {e}")
            return False

    def delete_service(self, name: str, namespace: str = "default") -> bool:
        """删除 Service"""
        if not self._initialized:
            return False
        try:
            self.core_v1.delete_namespaced_service(name, namespace)
            return True
        except ApiException:
            return False

    def _parse_service(self, svc) -> Dict[str, Any]:
        """解析 Service 对象"""
        spec = svc.spec
        ports = []
        if spec.ports:
            for p in spec.ports:
                ports.append({
                    "name": p.name,
                    "port": p.port,
                    "target_port": str(p.target_port) if p.target_port else None,
                    "node_port": p.node_port,
                    "protocol": p.protocol,
                })
        return {
            "name": svc.metadata.name,
            "namespace": svc.metadata.namespace,
            "labels": svc.metadata.labels or {},
            "type": spec.type,
            "cluster_ip": spec.cluster_ip,
            "external_ips": getattr(spec, 'external_i_ps', None) or [],
            "ports": ports,
            "selector": spec.selector or {},
            "created_at": svc.metadata.creation_timestamp.isoformat() if svc.metadata.creation_timestamp else None,
        }
    
    # ========== Namespace 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_namespaces(self) -> List[Dict[str, Any]]:
        """获取命名空间列表"""
        if not self._initialized:
            return []
        ns_list = self.core_v1.list_namespace()
        return [{
            "name": ns.metadata.name,
            "status": ns.status.phase if ns.status else "Active",
            "created_at": ns.metadata.creation_timestamp.isoformat() if ns.metadata.creation_timestamp else None,
        } for ns in ns_list.items]

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

    def get_namespace(self, name: str) -> Optional[Dict[str, Any]]:
        """获取命名空间详情"""
        if not self._initialized:
            return None
        try:
            ns = self.core_v1.read_namespace(name)
            return {
                "name": ns.metadata.name,
                "status": ns.status.phase if ns.status else "Active",
                "labels": ns.metadata.labels or {},
                "annotations": {k: v for k, v in (ns.metadata.annotations or {}).items()
                                if not k.startswith("kubectl.kubernetes.io/")},
                "created_at": ns.metadata.creation_timestamp.isoformat() if ns.metadata.creation_timestamp else None,
            }
        except ApiException as e:
            print(f"[K8s] Error getting namespace {name}: {e}")
            return None

    def create_namespace(self, name: str, labels: Optional[Dict[str, str]] = None) -> bool:
        """创建命名空间"""
        if not self._initialized:
            return False
        try:
            body = {"metadata": {"name": name}}
            if labels:
                body["metadata"]["labels"] = labels
            self.core_v1.create_namespace(body)
            return True
        except ApiException as e:
            print(f"[K8s] Error creating namespace {name}: {e}")
            return False

    def delete_namespace(self, name: str) -> bool:
        """删除命名空间"""
        if not self._initialized:
            return False
        try:
            self.core_v1.delete_namespace(name)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting namespace {name}: {e}")
            return False

    def get_namespace_resource_quota(self, namespace: str) -> Dict[str, Any]:
        """获取命名空间资源用量(Pod/Service/ConfigMap/Secret/PVC 计数)"""
        if not self._initialized:
            return {}
        result: Dict[str, Any] = {}
        try:
            # ResourceQuota
            quotas = self.core_v1.list_namespaced_resource_quota(namespace)
            if quotas.items:
                q = quotas.items[0]
                result["quota"] = {
                    "hard": q.status.hard if q.status and q.status.hard else {},
                    "used": q.status.used if q.status and q.status.used else {},
                }
        except Exception:
            pass
        try:
            pods = self.core_v1.list_namespaced_pod(namespace)
            result["pods"] = len(pods.items)
        except Exception:
            result["pods"] = 0
        try:
            svcs = self.core_v1.list_namespaced_service(namespace)
            result["services"] = len(svcs.items)
        except Exception:
            result["services"] = 0
        try:
            cms = self.core_v1.list_namespaced_config_map(namespace)
            result["configmaps"] = len(cms.items)
        except Exception:
            result["configmaps"] = 0
        try:
            secs = self.core_v1.list_namespaced_secret(namespace)
            result["secrets"] = len(secs.items)
        except Exception:
            result["secrets"] = 0
        return result

    # ========== ConfigMap 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_config_maps(self, namespace: str = "default",
                         all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 ConfigMap 列表"""
        if not self._initialized:
            return []
        try:
            if all_namespaces:
                cms = self.core_v1.list_config_map_for_all_namespaces()
            else:
                cms = self.core_v1.list_namespaced_config_map(namespace)
            return [self._parse_config_map(cm) for cm in cms.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing ConfigMaps: {e}")
            return []

    def get_config_map(self, name: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """获取 ConfigMap 详情"""
        if not self._initialized:
            return None
        try:
            cm = self.core_v1.read_namespaced_config_map(name, namespace)
            result = self._parse_config_map(cm)
            result["data"] = cm.data or {}
            result["binary_data_keys"] = list((cm.binary_data or {}).keys())
            return result
        except ApiException as e:
            print(f"[K8s] Error getting ConfigMap {namespace}/{name}: {e}")
            return None

    def delete_config_map(self, name: str, namespace: str = "default") -> bool:
        """删除 ConfigMap"""
        if not self._initialized:
            return False
        try:
            self.core_v1.delete_namespaced_config_map(name, namespace)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting ConfigMap {namespace}/{name}: {e}")
            return False

    def _parse_config_map(self, cm) -> Dict[str, Any]:
        """解析 ConfigMap 对象"""
        return {
            "name": cm.metadata.name,
            "namespace": cm.metadata.namespace,
            "labels": cm.metadata.labels or {},
            "key_count": len(cm.data or {}) + len(cm.binary_data or {}),
            "keys": list((cm.data or {}).keys()),
            "created_at": cm.metadata.creation_timestamp.isoformat() if cm.metadata.creation_timestamp else None,
        }

    # ========== Secret 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_secrets(self, namespace: str = "default",
                     all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 Secret 列表"""
        if not self._initialized:
            return []
        try:
            if all_namespaces:
                secs = self.core_v1.list_secret_for_all_namespaces()
            else:
                secs = self.core_v1.list_namespaced_secret(namespace)
            return [self._parse_secret(s) for s in secs.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing Secrets: {e}")
            return []

    def get_secret(self, name: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """获取 Secret 详情（值脱敏，仅返回 key 列表和长度）"""
        if not self._initialized:
            return None
        try:
            s = self.core_v1.read_namespaced_secret(name, namespace)
            result = self._parse_secret(s)
            # 脱敏：仅显示 key 名和值长度
            data_info = {}
            for k, v in (s.data or {}).items():
                data_info[k] = f"({len(v)} chars, base64)" if v else "(empty)"
            result["data_info"] = data_info
            return result
        except ApiException as e:
            print(f"[K8s] Error getting Secret {namespace}/{name}: {e}")
            return None

    def delete_secret(self, name: str, namespace: str = "default") -> bool:
        """删除 Secret"""
        if not self._initialized:
            return False
        try:
            self.core_v1.delete_namespaced_secret(name, namespace)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting Secret {namespace}/{name}: {e}")
            return False

    def _parse_secret(self, s) -> Dict[str, Any]:
        """解析 Secret 对象"""
        return {
            "name": s.metadata.name,
            "namespace": s.metadata.namespace,
            "type": s.type or "Opaque",
            "labels": s.metadata.labels or {},
            "key_count": len(s.data or {}),
            "keys": list((s.data or {}).keys()),
            "created_at": s.metadata.creation_timestamp.isoformat() if s.metadata.creation_timestamp else None,
        }

    # ========== Deployment 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_deployments(self, namespace: str = "default", label_selector: Optional[str] = None,
                         all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 Deployment 列表"""
        if not self._initialized:
            return []
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if all_namespaces:
                deps = self.apps_v1.list_deployment_for_all_namespaces(**kwargs)
            else:
                deps = self.apps_v1.list_namespaced_deployment(namespace, **kwargs)
            return [self._parse_deployment(d) for d in deps.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing deployments: {e}")
            return []

    @_k8s_retry(max_retries=1, delay=0.5)
    def create_deployment(self, namespace: str, deployment_spec: Dict) -> Optional[str]:
        """创建 Deployment"""
        if not self._initialized:
            return None
        try:
            result = self.apps_v1.create_namespaced_deployment(namespace, deployment_spec)
            return result.metadata.name
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error creating deployment: {e}")
            return None

    def get_deployment(self, name: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """获取 Deployment 详情"""
        if not self._initialized:
            return None
        try:
            dep = self.apps_v1.read_namespaced_deployment(name, namespace)
            return self._parse_deployment(dep)
        except ApiException as e:
            print(f"[K8s] Error getting deployment {name}: {e}")
            return None

    def update_deployment(self, name: str, namespace: str, spec: Dict) -> bool:
        """更新 Deployment"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.patch_namespaced_deployment(name, namespace, spec)
            return True
        except ApiException as e:
            print(f"[K8s] Error updating deployment {name}: {e}")
            return False

    @_k8s_retry(max_retries=1, delay=0.5)
    def delete_deployment(self, name: str, namespace: str = "default") -> bool:
        """删除 Deployment"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.delete_namespaced_deployment(name, namespace)
            return True
        except ApiException as e:
            if e.status == 0:
                raise
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

    def restart_deployment(self, name: str, namespace: str) -> bool:
        """滚动重启 Deployment"""
        if not self._initialized:
            return False
        try:
            import datetime
            now = datetime.datetime.utcnow().isoformat() + "Z"
            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": now
                            }
                        }
                    }
                }
            }
            self.apps_v1.patch_namespaced_deployment(name, namespace, body)
            return True
        except ApiException as e:
            print(f"[K8s] Error restarting deployment {name}: {e}")
            return False

    def _parse_deployment(self, dep) -> Dict[str, Any]:
        """解析 Deployment 对象"""
        images = []
        if dep.spec.template and dep.spec.template.spec:
            for c in dep.spec.template.spec.containers:
                images.append(c.image)
        conditions = []
        if dep.status.conditions:
            for cond in dep.status.conditions:
                conditions.append({
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                })
        annotations = dep.metadata.annotations or {}
        labels = dep.metadata.labels or {}
        return {
            "name": dep.metadata.name,
            "namespace": dep.metadata.namespace,
            "instance_name": annotations.get("lmaicloud/instance-name", ""),
            "instance_id": labels.get("instance-id", ""),
            "openclaw_instance_id": labels.get("openclaw-instance", ""),
            "annotations": annotations,
            "replicas": dep.spec.replicas or 0,
            "ready_replicas": dep.status.ready_replicas or 0,
            "available_replicas": dep.status.available_replicas or 0,
            "updated_replicas": dep.status.updated_replicas or 0,
            "labels": dep.metadata.labels or {},
            "selector": dep.spec.selector.match_labels if dep.spec.selector else {},
            "images": images,
            "conditions": conditions,
            "strategy": dep.spec.strategy.type if dep.spec.strategy else "RollingUpdate",
            "created_at": dep.metadata.creation_timestamp.isoformat() if dep.metadata.creation_timestamp else None,
        }

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

    # ========== PersistentVolume 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_pvs(self) -> List[Dict[str, Any]]:
        """获取 PersistentVolume 列表"""
        if not self._initialized:
            return []
        try:
            pvs = self.core_v1.list_persistent_volume()
            return [self._parse_pv(pv) for pv in pvs.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing PVs: {e}")
            return []

    def get_pv(self, name: str) -> Optional[Dict[str, Any]]:
        """获取 PV 详情"""
        if not self._initialized:
            return None
        try:
            pv = self.core_v1.read_persistent_volume(name)
            return self._parse_pv(pv)
        except ApiException as e:
            print(f"[K8s] Error getting PV {name}: {e}")
            return None

    def delete_pv(self, name: str) -> bool:
        """删除 PV"""
        if not self._initialized:
            return False
        try:
            self.core_v1.delete_persistent_volume(name)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting PV {name}: {e}")
            return False

    def create_pv(self, body: Dict[str, Any]) -> bool:
        """创建 PersistentVolume"""
        if not self._initialized:
            return False
        try:
            self.core_v1.create_persistent_volume(body=body)
            return True
        except ApiException as e:
            print(f"[K8s] Error creating PV: {e}")
            return False

    def _parse_pv(self, pv) -> Dict[str, Any]:
        """解析 PV 对象"""
        spec = pv.spec
        claim_ref = spec.claim_ref
        return {
            "name": pv.metadata.name,
            "labels": pv.metadata.labels or {},
            "capacity": spec.capacity.get("storage", "") if spec.capacity else "",
            "access_modes": spec.access_modes or [],
            "reclaim_policy": spec.persistent_volume_reclaim_policy or "",
            "status": pv.status.phase if pv.status else "Unknown",
            "storage_class": spec.storage_class_name or "",
            "claim": f"{claim_ref.namespace}/{claim_ref.name}" if claim_ref else "",
            "volume_mode": spec.volume_mode or "Filesystem",
            "reason": pv.status.reason if pv.status and pv.status.reason else "",
            "created_at": pv.metadata.creation_timestamp.isoformat() if pv.metadata.creation_timestamp else None,
        }

    # ========== PersistentVolumeClaim 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_pvcs(self, namespace: str = "default", all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 PVC 列表"""
        if not self._initialized:
            return []
        try:
            if all_namespaces:
                pvcs = self.core_v1.list_persistent_volume_claim_for_all_namespaces()
            else:
                pvcs = self.core_v1.list_namespaced_persistent_volume_claim(namespace)
            return [self._parse_pvc(pvc) for pvc in pvcs.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing PVCs: {e}")
            return []

    def get_pvc(self, name: str, namespace: str = "default") -> Optional[Dict[str, Any]]:
        """获取 PVC 详情"""
        if not self._initialized:
            return None
        try:
            pvc = self.core_v1.read_namespaced_persistent_volume_claim(name, namespace)
            return self._parse_pvc(pvc)
        except ApiException as e:
            print(f"[K8s] Error getting PVC {namespace}/{name}: {e}")
            return None

    def delete_pvc(self, name: str, namespace: str = "default") -> bool:
        """删除 PVC"""
        if not self._initialized:
            return False
        try:
            self.core_v1.delete_namespaced_persistent_volume_claim(name, namespace)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting PVC {namespace}/{name}: {e}")
            return False

    def _parse_pvc(self, pvc) -> Dict[str, Any]:
        """解析 PVC 对象"""
        spec = pvc.spec
        status = pvc.status
        capacity = status.capacity.get("storage", "") if status and status.capacity else ""
        return {
            "name": pvc.metadata.name,
            "namespace": pvc.metadata.namespace,
            "labels": pvc.metadata.labels or {},
            "status": status.phase if status else "Unknown",
            "volume": spec.volume_name or "",
            "capacity": capacity,
            "request": spec.resources.requests.get("storage", "") if spec.resources and spec.resources.requests else "",
            "access_modes": spec.access_modes or [],
            "storage_class": spec.storage_class_name or "",
            "volume_mode": spec.volume_mode or "Filesystem",
            "created_at": pvc.metadata.creation_timestamp.isoformat() if pvc.metadata.creation_timestamp else None,
        }

    # ========== 集群健康 & 指标 ==========

    def get_cluster_health(self) -> Dict[str, Any]:
        """获取集群健康检查（等价于 kubectl get --raw='/readyz?verbose'）"""
        if not self._initialized:
            return {"status": "unknown", "checks": []}
        try:
            # 使用 raw API 调用 /readyz?verbose
            api_client = self._api_client
            resp = api_client.call_api(
                '/readyz', 'GET',
                query_params=[('verbose', '')],
                response_type='str',
                auth_settings=['BearerToken'],
                _return_http_data_only=False,
                _preload_content=True,
            )
            body = resp[0] if resp else ""
            status_code = resp[1] if len(resp) > 1 else 200
            # 解析 verbose 输出：每行格式为 "[+]component-name ok" 或 "[-]component-name failed: ..."
            checks = []
            lines = body.strip().split('\n') if body else []
            overall_status = "ok"
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line.startswith('[+]'):
                    name = line[3:].split(' ')[0].strip()
                    checks.append({"name": name, "status": "ok", "message": line[3:].strip()})
                elif line.startswith('[-]'):
                    name = line[3:].split(' ')[0].strip()
                    checks.append({"name": name, "status": "failed", "message": line[3:].strip()})
                    overall_status = "degraded"
                elif line.startswith('readyz check'):
                    if 'failed' in line.lower():
                        overall_status = "unhealthy"
                    # 最后的汇总行
                    checks.append({"name": "_summary", "status": overall_status, "message": line})
            return {"status": overall_status, "checks": checks}
        except Exception as e:
            # 回退: 通过 componentstatuses 和 livez 接口判断
            return self._get_health_fallback()

    def _get_health_fallback(self) -> Dict[str, Any]:
        """健康检查回退方案: 通过节点状态和 API 可达性判断"""
        checks = []
        overall = "ok"
        try:
            # 检查 API Server 是否可达
            ver = self.core_v1.get_api_versions()
            checks.append({"name": "api-server", "status": "ok", "message": "API server reachable"})
        except Exception:
            checks.append({"name": "api-server", "status": "failed", "message": "API server unreachable"})
            overall = "unhealthy"
        try:
            nodes = self.core_v1.list_node()
            total = len(nodes.items)
            ready = sum(1 for n in nodes.items
                        if n.status and n.status.conditions
                        and any(c.type == 'Ready' and c.status == 'True' for c in n.status.conditions))
            status = "ok" if ready == total else ("degraded" if ready > 0 else "failed")
            if status != "ok":
                overall = "degraded"
            checks.append({"name": "nodes", "status": status, "message": f"{ready}/{total} nodes ready"})
        except Exception:
            checks.append({"name": "nodes", "status": "failed", "message": "Cannot list nodes"})
            overall = "unhealthy"
        return {"status": overall, "checks": checks}

    def list_node_metrics(self) -> List[Dict[str, Any]]:
        """获取所有节点资源指标（等价于 kubectl top node）"""
        if not self._initialized:
            return []
        try:
            metrics_list = self.custom_objects.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="nodes",
            )
            results = []
            for item in metrics_list.get("items", []):
                name = item.get("metadata", {}).get("name", "")
                usage = item.get("usage", {})
                cpu_raw = usage.get("cpu", "0")
                mem_raw = usage.get("memory", "0")
                # 解析 CPU: 可能是 "250m" 或 "1" (核)
                if cpu_raw.endswith('n'):
                    cpu_millicores = int(cpu_raw[:-1]) // 1000000
                elif cpu_raw.endswith('u'):
                    cpu_millicores = int(cpu_raw[:-1]) // 1000
                elif cpu_raw.endswith('m'):
                    cpu_millicores = int(cpu_raw[:-1])
                else:
                    cpu_millicores = int(cpu_raw) * 1000 if cpu_raw.isdigit() else 0
                # 解析内存: Ki/Mi/Gi
                if mem_raw.endswith('Ki'):
                    mem_bytes = int(mem_raw[:-2]) * 1024
                elif mem_raw.endswith('Mi'):
                    mem_bytes = int(mem_raw[:-2]) * 1024 * 1024
                elif mem_raw.endswith('Gi'):
                    mem_bytes = int(mem_raw[:-2]) * 1024 * 1024 * 1024
                elif mem_raw.isdigit():
                    mem_bytes = int(mem_raw)
                else:
                    mem_bytes = 0
                results.append({
                    "name": name,
                    "cpu_usage_millicores": cpu_millicores,
                    "memory_usage_bytes": mem_bytes,
                    "cpu_raw": cpu_raw,
                    "memory_raw": mem_raw,
                    "timestamp": item.get("timestamp"),
                })
            return results
        except Exception as e:
            print(f"[K8s] Error listing node metrics: {e}")
            return []

    def list_pod_metrics(self, namespace: str = "", all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 Pod 资源指标（等价于 kubectl top pod）"""
        if not self._initialized:
            return []
        try:
            if all_namespaces or not namespace:
                metrics_list = self.custom_objects.list_cluster_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    plural="pods",
                )
            else:
                metrics_list = self.custom_objects.list_namespaced_custom_object(
                    group="metrics.k8s.io",
                    version="v1beta1",
                    namespace=namespace,
                    plural="pods",
                )
            results = []
            for item in metrics_list.get("items", []):
                meta = item.get("metadata", {})
                pod_name = meta.get("name", "")
                pod_ns = meta.get("namespace", "")
                # 累加所有容器的 CPU 和内存
                total_cpu_mc = 0
                total_mem_bytes = 0
                for container in item.get("containers", []):
                    usage = container.get("usage", {})
                    cpu_raw = usage.get("cpu", "0")
                    mem_raw = usage.get("memory", "0")
                    # 解析 CPU
                    if cpu_raw.endswith('n'):
                        total_cpu_mc += int(cpu_raw[:-1]) // 1000000
                    elif cpu_raw.endswith('u'):
                        total_cpu_mc += int(cpu_raw[:-1]) // 1000
                    elif cpu_raw.endswith('m'):
                        total_cpu_mc += int(cpu_raw[:-1])
                    elif cpu_raw.isdigit():
                        total_cpu_mc += int(cpu_raw) * 1000
                    # 解析内存
                    if mem_raw.endswith('Ki'):
                        total_mem_bytes += int(mem_raw[:-2]) * 1024
                    elif mem_raw.endswith('Mi'):
                        total_mem_bytes += int(mem_raw[:-2]) * 1024 * 1024
                    elif mem_raw.endswith('Gi'):
                        total_mem_bytes += int(mem_raw[:-2]) * 1024 * 1024 * 1024
                    elif mem_raw.isdigit():
                        total_mem_bytes += int(mem_raw)
                results.append({
                    "name": pod_name,
                    "namespace": pod_ns,
                    "cpu_usage_millicores": total_cpu_mc,
                    "memory_usage_bytes": total_mem_bytes,
                    "timestamp": item.get("timestamp"),
                })
            return results
        except Exception as e:
            print(f"[K8s] Error listing pod metrics: {e}")
            return []

    def get_cluster_version(self) -> Dict[str, Any]:
        """获取集群版本信息"""
        if not self._initialized:
            return {}
        try:
            from kubernetes.client import VersionApi
            ver_api = VersionApi(self._api_client)
            ver = ver_api.get_code()
            return {
                "major": ver.major,
                "minor": ver.minor,
                "git_version": ver.git_version,
                "platform": ver.platform,
                "go_version": ver.go_version,
                "build_date": ver.build_date,
            }
        except Exception as e:
            print(f"[K8s] Error getting cluster version: {e}")
            return {}

    def list_warning_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取集群级别的 Warning 事件"""
        if not self._initialized:
            return []
        try:
            events = self.core_v1.list_event_for_all_namespaces(
                field_selector="type=Warning",
                limit=limit,
            )
            results = []
            for e in events.items:
                results.append({
                    "namespace": e.metadata.namespace,
                    "name": e.involved_object.name if e.involved_object else "",
                    "kind": e.involved_object.kind if e.involved_object else "",
                    "reason": e.reason,
                    "message": e.message,
                    "count": e.count or 1,
                    "type": e.type,
                    "first_timestamp": e.first_timestamp.isoformat() if e.first_timestamp else None,
                    "last_timestamp": e.last_timestamp.isoformat() if e.last_timestamp else None,
                })
            # 按最后发生时间倒序
            results.sort(key=lambda x: x.get("last_timestamp") or "", reverse=True)
            return results
        except Exception as e:
            print(f"[K8s] Error listing warning events: {e}")
            return []

    # ========== DaemonSet 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_daemon_sets(self, namespace: str = "default", label_selector: Optional[str] = None,
                         all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 DaemonSet 列表"""
        if not self._initialized:
            return []
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if all_namespaces:
                ds_list = self.apps_v1.list_daemon_set_for_all_namespaces(**kwargs)
            else:
                ds_list = self.apps_v1.list_namespaced_daemon_set(namespace, **kwargs)
            return [self._parse_daemon_set(ds) for ds in ds_list.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing DaemonSets: {e}")
            return []

    def delete_daemon_set(self, name: str, namespace: str = "default") -> bool:
        """删除 DaemonSet"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.delete_namespaced_daemon_set(name, namespace)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting DaemonSet {name}: {e}")
            return False

    def restart_daemon_set(self, name: str, namespace: str) -> bool:
        """滚动重启 DaemonSet"""
        if not self._initialized:
            return False
        try:
            import datetime
            now = datetime.datetime.utcnow().isoformat() + "Z"
            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": now
                            }
                        }
                    }
                }
            }
            self.apps_v1.patch_namespaced_daemon_set(name, namespace, body)
            return True
        except ApiException as e:
            print(f"[K8s] Error restarting DaemonSet {name}: {e}")
            return False

    def _parse_daemon_set(self, ds) -> Dict[str, Any]:
        """解析 DaemonSet 对象"""
        images = []
        if ds.spec.template and ds.spec.template.spec:
            for c in ds.spec.template.spec.containers:
                images.append(c.image)
        conditions = []
        if ds.status.conditions:
            for cond in ds.status.conditions:
                conditions.append({
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                })
        annotations = ds.metadata.annotations or {}
        return {
            "name": ds.metadata.name,
            "namespace": ds.metadata.namespace,
            "instance_name": annotations.get("lmaicloud/instance-name", ""),
            "desired_number_scheduled": ds.status.desired_number_scheduled or 0,
            "current_number_scheduled": ds.status.current_number_scheduled or 0,
            "number_ready": ds.status.number_ready or 0,
            "number_available": ds.status.number_available or 0,
            "number_misscheduled": ds.status.number_misscheduled or 0,
            "updated_number_scheduled": ds.status.updated_number_scheduled or 0,
            "labels": ds.metadata.labels or {},
            "images": images,
            "conditions": conditions,
            "created_at": ds.metadata.creation_timestamp.isoformat() if ds.metadata.creation_timestamp else None,
        }

    # ========== StatefulSet 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_stateful_sets(self, namespace: str = "default", label_selector: Optional[str] = None,
                           all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 StatefulSet 列表"""
        if not self._initialized:
            return []
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if all_namespaces:
                ss_list = self.apps_v1.list_stateful_set_for_all_namespaces(**kwargs)
            else:
                ss_list = self.apps_v1.list_namespaced_stateful_set(namespace, **kwargs)
            return [self._parse_stateful_set(ss) for ss in ss_list.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing StatefulSets: {e}")
            return []

    def delete_stateful_set(self, name: str, namespace: str = "default") -> bool:
        """删除 StatefulSet"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.delete_namespaced_stateful_set(name, namespace)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting StatefulSet {name}: {e}")
            return False

    def scale_stateful_set(self, name: str, namespace: str, replicas: int) -> bool:
        """调整 StatefulSet 副本数"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.patch_namespaced_stateful_set_scale(
                name, namespace, {"spec": {"replicas": replicas}}
            )
            return True
        except ApiException as e:
            print(f"[K8s] Error scaling StatefulSet {name}: {e}")
            return False

    def restart_stateful_set(self, name: str, namespace: str) -> bool:
        """滚动重启 StatefulSet"""
        if not self._initialized:
            return False
        try:
            import datetime
            now = datetime.datetime.utcnow().isoformat() + "Z"
            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": now
                            }
                        }
                    }
                }
            }
            self.apps_v1.patch_namespaced_stateful_set(name, namespace, body)
            return True
        except ApiException as e:
            print(f"[K8s] Error restarting StatefulSet {name}: {e}")
            return False

    def _parse_stateful_set(self, ss) -> Dict[str, Any]:
        """解析 StatefulSet 对象"""
        images = []
        if ss.spec.template and ss.spec.template.spec:
            for c in ss.spec.template.spec.containers:
                images.append(c.image)
        conditions = []
        if ss.status.conditions:
            for cond in ss.status.conditions:
                conditions.append({
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                })
        annotations = ss.metadata.annotations or {}
        return {
            "name": ss.metadata.name,
            "namespace": ss.metadata.namespace,
            "instance_name": annotations.get("lmaicloud/instance-name", ""),
            "replicas": ss.spec.replicas or 0,
            "ready_replicas": ss.status.ready_replicas or 0,
            "current_replicas": ss.status.current_replicas or 0,
            "updated_replicas": ss.status.updated_replicas or 0,
            "labels": ss.metadata.labels or {},
            "images": images,
            "conditions": conditions,
            "service_name": ss.spec.service_name or "",
            "update_strategy": ss.spec.update_strategy.type if ss.spec.update_strategy else "RollingUpdate",
            "created_at": ss.metadata.creation_timestamp.isoformat() if ss.metadata.creation_timestamp else None,
        }

    # ========== StorageClass 管理 ==========

    # ========== DaemonSet 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_daemon_sets(self, namespace: str = "default", label_selector: Optional[str] = None,
                         all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 DaemonSet 列表"""
        if not self._initialized:
            return []
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if all_namespaces:
                ds_list = self.apps_v1.list_daemon_set_for_all_namespaces(**kwargs)
            else:
                ds_list = self.apps_v1.list_namespaced_daemon_set(namespace, **kwargs)
            return [self._parse_daemon_set(ds) for ds in ds_list.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing DaemonSets: {e}")
            return []

    def delete_daemon_set(self, name: str, namespace: str = "default") -> bool:
        """删除 DaemonSet"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.delete_namespaced_daemon_set(name, namespace)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting DaemonSet {name}: {e}")
            return False

    def restart_daemon_set(self, name: str, namespace: str) -> bool:
        """滚动重启 DaemonSet"""
        if not self._initialized:
            return False
        try:
            import datetime
            now = datetime.datetime.utcnow().isoformat() + "Z"
            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": now
                            }
                        }
                    }
                }
            }
            self.apps_v1.patch_namespaced_daemon_set(name, namespace, body)
            return True
        except ApiException as e:
            print(f"[K8s] Error restarting DaemonSet {name}: {e}")
            return False

    def _parse_daemon_set(self, ds) -> Dict[str, Any]:
        """解析 DaemonSet 对象"""
        images = []
        if ds.spec.template and ds.spec.template.spec:
            for c in ds.spec.template.spec.containers:
                images.append(c.image)
        conditions = []
        if ds.status.conditions:
            for cond in ds.status.conditions:
                conditions.append({
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                })
        annotations = ds.metadata.annotations or {}
        return {
            "name": ds.metadata.name,
            "namespace": ds.metadata.namespace,
            "instance_name": annotations.get("lmaicloud/instance-name", ""),
            "desired_number_scheduled": ds.status.desired_number_scheduled or 0,
            "current_number_scheduled": ds.status.current_number_scheduled or 0,
            "number_ready": ds.status.number_ready or 0,
            "number_available": ds.status.number_available or 0,
            "number_misscheduled": ds.status.number_misscheduled or 0,
            "updated_number_scheduled": ds.status.updated_number_scheduled or 0,
            "labels": ds.metadata.labels or {},
            "images": images,
            "conditions": conditions,
            "created_at": ds.metadata.creation_timestamp.isoformat() if ds.metadata.creation_timestamp else None,
        }

    # ========== StatefulSet 管理 ==========

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_stateful_sets(self, namespace: str = "default", label_selector: Optional[str] = None,
                           all_namespaces: bool = False) -> List[Dict[str, Any]]:
        """获取 StatefulSet 列表"""
        if not self._initialized:
            return []
        try:
            kwargs = {}
            if label_selector:
                kwargs["label_selector"] = label_selector
            if all_namespaces:
                ss_list = self.apps_v1.list_stateful_set_for_all_namespaces(**kwargs)
            else:
                ss_list = self.apps_v1.list_namespaced_stateful_set(namespace, **kwargs)
            return [self._parse_stateful_set(ss) for ss in ss_list.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing StatefulSets: {e}")
            return []

    def delete_stateful_set(self, name: str, namespace: str = "default") -> bool:
        """删除 StatefulSet"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.delete_namespaced_stateful_set(name, namespace)
            return True
        except ApiException as e:
            print(f"[K8s] Error deleting StatefulSet {name}: {e}")
            return False

    def scale_stateful_set(self, name: str, namespace: str, replicas: int) -> bool:
        """调整 StatefulSet 副本数"""
        if not self._initialized:
            return False
        try:
            self.apps_v1.patch_namespaced_stateful_set_scale(
                name, namespace, {"spec": {"replicas": replicas}}
            )
            return True
        except ApiException as e:
            print(f"[K8s] Error scaling StatefulSet {name}: {e}")
            return False

    def restart_stateful_set(self, name: str, namespace: str) -> bool:
        """滚动重启 StatefulSet"""
        if not self._initialized:
            return False
        try:
            import datetime
            now = datetime.datetime.utcnow().isoformat() + "Z"
            body = {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "kubectl.kubernetes.io/restartedAt": now
                            }
                        }
                    }
                }
            }
            self.apps_v1.patch_namespaced_stateful_set(name, namespace, body)
            return True
        except ApiException as e:
            print(f"[K8s] Error restarting StatefulSet {name}: {e}")
            return False

    def _parse_stateful_set(self, ss) -> Dict[str, Any]:
        """解析 StatefulSet 对象"""
        images = []
        if ss.spec.template and ss.spec.template.spec:
            for c in ss.spec.template.spec.containers:
                images.append(c.image)
        conditions = []
        if ss.status.conditions:
            for cond in ss.status.conditions:
                conditions.append({
                    "type": cond.type,
                    "status": cond.status,
                    "reason": cond.reason,
                    "message": cond.message,
                })
        annotations = ss.metadata.annotations or {}
        return {
            "name": ss.metadata.name,
            "namespace": ss.metadata.namespace,
            "instance_name": annotations.get("lmaicloud/instance-name", ""),
            "replicas": ss.spec.replicas or 0,
            "ready_replicas": ss.status.ready_replicas or 0,
            "current_replicas": ss.status.current_replicas or 0,
            "updated_replicas": ss.status.updated_replicas or 0,
            "labels": ss.metadata.labels or {},
            "images": images,
            "conditions": conditions,
            "service_name": ss.spec.service_name or "",
            "update_strategy": ss.spec.update_strategy.type if ss.spec.update_strategy else "RollingUpdate",
            "created_at": ss.metadata.creation_timestamp.isoformat() if ss.metadata.creation_timestamp else None,
        }

    @_k8s_retry(max_retries=2, delay=1.0)
    def list_storage_classes(self) -> List[Dict[str, Any]]:
        """获取 StorageClass 列表"""
        if not self._initialized:
            return []
        try:
            from kubernetes.client import StorageV1Api
            storage_v1 = StorageV1Api(self._api_client)
            scs = storage_v1.list_storage_class()
            return [self._parse_sc(sc) for sc in scs.items]
        except ApiException as e:
            if e.status == 0:
                raise
            print(f"[K8s] Error listing StorageClasses: {e}")
            return []
        except Exception as e:
            print(f"[K8s] Error listing StorageClasses: {e}")
            return []

    def get_storage_class(self, name: str) -> Optional[Dict[str, Any]]:
        """获取 StorageClass 详情"""
        if not self._initialized:
            return None
        try:
            from kubernetes.client import StorageV1Api
            storage_v1 = StorageV1Api(self._api_client)
            sc = storage_v1.read_storage_class(name)
            return self._parse_sc(sc)
        except ApiException as e:
            print(f"[K8s] Error getting StorageClass {name}: {e}")
            return None

    def _parse_sc(self, sc) -> Dict[str, Any]:
        """解析 StorageClass 对象"""
        annotations = sc.metadata.annotations or {}
        is_default = annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
        return {
            "name": sc.metadata.name,
            "labels": sc.metadata.labels or {},
            "provisioner": sc.provisioner or "",
            "reclaim_policy": sc.reclaim_policy or "",
            "volume_binding_mode": sc.volume_binding_mode or "",
            "allow_volume_expansion": sc.allow_volume_expansion or False,
            "is_default": is_default,
            "parameters": sc.parameters or {},
            "created_at": sc.metadata.creation_timestamp.isoformat() if sc.metadata.creation_timestamp else None,
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
