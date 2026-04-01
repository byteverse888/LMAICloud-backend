"""
监控告警服务

负责节点/实例健康检查和告警
"""
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

from app.services.k8s_client import get_k8s_client


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertType(str, Enum):
    NODE_DOWN = "node_down"
    NODE_HIGH_CPU = "node_high_cpu"
    NODE_HIGH_MEMORY = "node_high_memory"
    INSTANCE_UNHEALTHY = "instance_unhealthy"
    BALANCE_LOW = "balance_low"


class MonitoringService:
    """监控服务"""
    
    CPU_WARNING = 80
    CPU_CRITICAL = 95
    MEMORY_WARNING = 85
    MEMORY_CRITICAL = 95
    
    def __init__(self):
        self.k8s = get_k8s_client()
        self._alerts: List[Dict[str, Any]] = []
    
    def check_node_health(self, node_name: str) -> Dict[str, Any]:
        """检查节点健康状态"""
        node = self.k8s.get_node(node_name)
        if not node:
            return {"healthy": False, "reason": "Node not found"}
        
        issues = []
        if node["status"] != "Ready":
            issues.append(f"Node status: {node['status']}")
            self._add_alert(AlertType.NODE_DOWN, AlertLevel.CRITICAL, f"Node {node_name} is not ready")
        
        return {
            "healthy": len(issues) == 0,
            "node_name": node_name,
            "status": node["status"],
            "issues": issues,
            "checked_at": datetime.utcnow().isoformat(),
        }
    
    def check_all_nodes(self) -> Dict[str, Any]:
        """检查所有节点"""
        nodes = self.k8s.list_nodes()
        healthy = sum(1 for n in nodes if n["status"] == "Ready")
        
        return {
            "total": len(nodes),
            "healthy": healthy,
            "unhealthy": len(nodes) - healthy,
            "checked_at": datetime.utcnow().isoformat(),
        }
    
    def check_instance_health(self, instance_id: str, namespace: str = "lmaicloud") -> Dict[str, Any]:
        """检查实例健康"""
        pod = self.k8s.get_pod(f"inst-{instance_id[:8]}", namespace)
        if not pod:
            return {"healthy": False, "reason": "Instance not found"}
        
        healthy = pod["status"] in ["Running", "Succeeded"]
        if not healthy:
            self._add_alert(AlertType.INSTANCE_UNHEALTHY, AlertLevel.WARNING, f"Instance {instance_id} status: {pod['status']}")
        
        return {
            "healthy": healthy,
            "instance_id": instance_id,
            "status": pod["status"],
            "checked_at": datetime.utcnow().isoformat(),
        }
    
    def _add_alert(self, alert_type: AlertType, level: AlertLevel, message: str):
        """添加告警"""
        self._alerts.append({
            "id": f"{alert_type.value}-{datetime.utcnow().timestamp()}",
            "type": alert_type.value,
            "level": level.value,
            "message": message,
            "created_at": datetime.utcnow().isoformat(),
            "acknowledged": False,
        })
    
    def get_alerts(self, level: Optional[AlertLevel] = None) -> List[Dict]:
        """获取告警列表"""
        alerts = [a for a in self._alerts if not a["acknowledged"]]
        if level:
            alerts = [a for a in alerts if a["level"] == level.value]
        return alerts
    
    def ack_alert(self, alert_id: str) -> bool:
        """确认告警"""
        for alert in self._alerts:
            if alert["id"] == alert_id:
                alert["acknowledged"] = True
                return True
        return False


# ========== 单例 ==========

_monitoring: Optional[MonitoringService] = None


def get_monitoring_service() -> MonitoringService:
    global _monitoring
    if _monitoring is None:
        _monitoring = MonitoringService()
    return _monitoring
