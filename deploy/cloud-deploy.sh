#!/bin/bash
#===============================================================================
# LMAICloud 云主机 K8s + KubeEdge 一键部署脚本
# 系统要求: Ubuntu 22.04/24.04 LTS
# 功能: K8s 单节点集群部署 + KubeEdge CloudCore 部署
# 版本: K8s v1.35.x + KubeEdge v1.22.x
#===============================================================================

set -e

#===============================================================================
# 配置区域 - 根据实际环境修改
#===============================================================================
# K8s 版本 (留空则自动获取最新稳定版)
# 最新稳定版: v1.35.1 (2026-02)
K8S_VERSION=""
# KubeEdge 版本 (留空则自动获取最新稳定版)
# 最新稳定版: v1.22.1 (2025-12)
KUBEEDGE_VERSION=""
# Pod 网络 CIDR
POD_CIDR="10.244.0.0/16"
# Service 网络 CIDR
SERVICE_CIDR="10.96.0.0/12"
# 云主机公网IP (用于边缘节点连接，必须设置)
CLOUD_PUBLIC_IP=""
# CloudCore WebSocket端口
CLOUDCORE_PORT="10000"
# CloudCore QUIC端口
CLOUDCORE_QUIC_PORT="10001"
# CloudCore HTTPS端口
CLOUDCORE_HTTPS_PORT="10002"
# CloudCore Stream端口 (用于 kubectl exec/logs)
CLOUDCORE_STREAM_PORT="10003"
# CloudCore Tunnel端口
CLOUDCORE_TUNNEL_PORT="10004"
# kubeconfig 路径
KUBE_CONFIG="/root/.kube/config"
# 容器运行时 (containerd)
CONTAINER_RUNTIME="containerd"
# 国内镜像加速
USE_CN_MIRROR=true

#===============================================================================
# 颜色定义
#===============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

#===============================================================================
# 日志配置
#===============================================================================
# 日志配置 - 普通用户使用当前目录
if [[ $EUID -eq 0 ]]; then
    LOG_DIR="/var/log/kubeedge-deploy"
else
    LOG_DIR="./logs"
fi
mkdir -p "$LOG_DIR" 2>/dev/null || LOG_DIR="."
LOG_FILE="$LOG_DIR/cloud-deploy-$(date '+%Y%m%d-%H%M%S').log"
touch "$LOG_FILE" 2>/dev/null || LOG_FILE="/dev/null"

# 日志函数 - 同时输出到终端和文件
log_info()  { echo -e "${GREEN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $(date '+%Y-%m-%d %H:%M:%S') ========== $* ==========" | tee -a "$LOG_FILE"; }

# 执行命令并记录日志，失败时输出详细信息
run_cmd() {
    local cmd="$*"
    echo "[CMD] $cmd" >> "$LOG_FILE"
    local output
    output=$(eval "$cmd" 2>&1)
    local exit_code=$?
    echo "$output" >> "$LOG_FILE"
    if [[ $exit_code -ne 0 ]]; then
        log_error "命令失败: $cmd"
        log_error "退出码: $exit_code"
        log_error "输出: $output"
        return $exit_code
    fi
    echo "$output"
    return 0
}

#===============================================================================
# 帮助信息
#===============================================================================
show_help() {
    cat << EOF
用法: $0 [命令] [选项]

命令:
    install         完整安装 K8s + KubeEdge
    install-k8s     仅安装 K8s
    install-kubeedge 仅安装 KubeEdge (需要先安装 K8s)
    uninstall       完全卸载 K8s + KubeEdge
    reset           重置集群 (保留已安装的组件)
    status          查看集群状态
    token           生成边缘节点加入 token
    help            显示帮助信息

选项:
    --cloud-ip IP   设置云主机公网IP (必须)
    --k8s-version   指定 K8s 版本 (如 1.31.0)
    --ke-version    指定 KubeEdge 版本 (如 1.19.0)
    --no-cn-mirror  禁用国内镜像加速

示例:
    $0 install --cloud-ip 1.2.3.4
    $0 install --cloud-ip 1.2.3.4 --k8s-version 1.31.0
    $0 token
    $0 reset
    $0 uninstall

EOF
}

#===============================================================================
# 参数解析
#===============================================================================
parse_args() {
    COMMAND=""
    while [[ $# -gt 0 ]]; do
        case $1 in
            install|install-k8s|install-kubeedge|uninstall|reset|status|token|help)
                COMMAND=$1
                shift
                ;;
            -h|--help)
                show_help
                exit 0
                ;;
            --cloud-ip)
                CLOUD_PUBLIC_IP="$2"
                shift 2
                ;;
            --k8s-version)
                K8S_VERSION="$2"
                shift 2
                ;;
            --ke-version)
                KUBEEDGE_VERSION="$2"
                shift 2
                ;;
            --no-cn-mirror)
                USE_CN_MIRROR=false
                shift
                ;;
            *)
                log_error "未知参数: $1"
                show_help
                exit 1
                ;;
        esac
    done

    if [[ -z "$COMMAND" ]]; then
        show_help
        exit 1
    fi
}

#===============================================================================
# 系统检查
#===============================================================================
check_system() {
    log_step "系统环境检查"
    
    # 检查是否为 root
    if [[ $EUID -ne 0 ]]; then
        log_error "请使用 root 用户运行此脚本"
        exit 1
    fi
    
    # 确保 HOME 指向 /root（sudo 执行时可能不是）
    export HOME=/root
    
    # 检查系统版本
    if [[ ! -f /etc/os-release ]]; then
        log_error "无法检测操作系统版本"
        exit 1
    fi
    
    source /etc/os-release
    log_info "操作系统: $PRETTY_NAME"
    
    if [[ "$ID" != "ubuntu" ]] || [[ ! "$VERSION_ID" =~ ^(22|24)\.04 ]]; then
        log_warn "推荐使用 Ubuntu 22.04/24.04 LTS，当前系统: $PRETTY_NAME"
    fi
    
    # 检查 CPU 和内存
    CPU_CORES=$(nproc)
    MEM_TOTAL=$(free -g | awk '/^Mem:/{print $2}')
    log_info "CPU 核心数: $CPU_CORES"
    log_info "内存大小: ${MEM_TOTAL}GB"
    
    if [[ $CPU_CORES -lt 2 ]]; then
        log_warn "K8s 推荐至少 2 核 CPU"
    fi
    
    if [[ $MEM_TOTAL -lt 2 ]]; then
        log_warn "K8s 推荐至少 2GB 内存"
    fi
    
    # 检查网络
    if ! ping -c 1 -W 3 8.8.8.8 &>/dev/null && ! ping -c 1 -W 3 114.114.114.114 &>/dev/null; then
        log_error "网络连接失败，请检查网络配置"
        exit 1
    fi
    log_info "网络连接正常"
}

#===============================================================================
# 检查并配置公网IP
#===============================================================================
check_public_ip() {
    log_step "检查公网IP配置"
    
    if [[ -z "$CLOUD_PUBLIC_IP" ]]; then
        log_warn "未指定公网IP，跳过检查"
        return 0
    fi
    
    # 检查公网IP是否已配置到本机网络接口
    if ip addr | grep -q "$CLOUD_PUBLIC_IP"; then
        log_info "公网IP $CLOUD_PUBLIC_IP 已配置到本机网络接口"
        return 0
    fi
    
    log_warn "公网IP $CLOUD_PUBLIC_IP 未配置到本机网络接口"
    log_warn "这可能导致边缘节点无法连接云端"
    
    # 获取默认网络接口
    DEFAULT_IFACE=$(ip route | grep default | awk '{print $5}' | head -1)
    if [[ -z "$DEFAULT_IFACE" ]]; then
        DEFAULT_IFACE="eth0"
    fi
    
    echo ""
    echo "============================================================"
    echo -e "${YELLOW}公网IP $CLOUD_PUBLIC_IP 未配置到本机${NC}"
    echo "============================================================"
    echo ""
    
    read -p "是否自动配置公网IP并持久化到 netplan? (y/N): " auto_config
    if [[ "$auto_config" == "y" || "$auto_config" == "Y" ]]; then
        configure_public_ip_netplan
    else
        log_warn "跳过公网IP配置"
        log_warn "请手动执行: sudo ip addr add ${CLOUD_PUBLIC_IP}/24 dev ${DEFAULT_IFACE}"
    fi
}

#===============================================================================
# 通过 netplan 持久化配置公网IP
#===============================================================================
configure_public_ip_netplan() {
    log_info "配置公网IP并持久化到 netplan..."
    
    # 获取默认网络接口
    DEFAULT_IFACE=$(ip route | grep default | awk '{print $5}' | head -1)
    if [[ -z "$DEFAULT_IFACE" ]]; then
        DEFAULT_IFACE="eth0"
    fi
    
    # 获取当前内网IP和网关
    CURRENT_IP=$(ip addr show $DEFAULT_IFACE | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | head -1)
    GATEWAY=$(ip route | grep default | awk '{print $3}' | head -1)
    
    if [[ -z "$CURRENT_IP" ]]; then
        log_error "无法获取当前内网IP"
        exit 1
    fi
    
    log_info "当前网络接口: $DEFAULT_IFACE"
    log_info "当前内网IP: $CURRENT_IP"
    log_info "网关: $GATEWAY"
    log_info "要添加的公网IP: $CLOUD_PUBLIC_IP/24"
    
    # 查找现有的 netplan 配置文件
    NETPLAN_FILE=$(ls /etc/netplan/*.yaml 2>/dev/null | head -1)
    if [[ -z "$NETPLAN_FILE" ]]; then
        NETPLAN_FILE="/etc/netplan/01-kubeedge-public-ip.yaml"
        log_info "创建新的 netplan 配置文件: $NETPLAN_FILE"
    else
        log_info "使用现有 netplan 配置文件: $NETPLAN_FILE"
    fi
    
    # 检查公网IP是否已在配置文件中
    if grep -q "$CLOUD_PUBLIC_IP" "$NETPLAN_FILE" 2>/dev/null; then
        log_info "公网IP已在 netplan 配置中，应用配置..."
        netplan apply 2>/dev/null || true
    else
        # 备份现有配置
        if [[ -f "$NETPLAN_FILE" ]]; then
            cp "$NETPLAN_FILE" "${NETPLAN_FILE}.bak.$(date +%Y%m%d%H%M%S)"
            log_info "已备份原配置文件"
        fi
        
        # 生成新的 netplan 配置
        cat > "$NETPLAN_FILE" << EOF
# KubeEdge 云端网络配置 - 自动生成于 $(date '+%Y-%m-%d %H:%M:%S')
# 公网IP: $CLOUD_PUBLIC_IP
network:
  version: 2
  renderer: networkd
  ethernets:
    $DEFAULT_IFACE:
      addresses:
        - $CURRENT_IP
        - ${CLOUD_PUBLIC_IP}/24
      routes:
        - to: default
          via: $GATEWAY
      nameservers:
        addresses:
          - 8.8.8.8
          - 114.114.114.114
EOF
        
        log_info "netplan 配置文件已更新"
        
        # 应用配置
        log_info "应用 netplan 配置..."
        netplan apply 2>&1 | tee -a "$LOG_FILE" || {
            log_error "netplan apply 失败，尝试手动添加IP..."
            ip addr add ${CLOUD_PUBLIC_IP}/24 dev ${DEFAULT_IFACE} 2>/dev/null || true
        }
    fi
    
    # 验证配置
    sleep 2
    if ip addr | grep -q "$CLOUD_PUBLIC_IP"; then
        log_info "公网IP配置成功并已持久化"
    else
        log_error "公网IP配置失败"
        log_error "请手动检查 $NETPLAN_FILE"
        exit 1
    fi
}

#===============================================================================
# 获取最新版本
#===============================================================================
get_latest_versions() {
    log_step "获取最新稳定版本"
    
    # 获取 K8s 最新稳定版
    if [[ -z "$K8S_VERSION" ]]; then
        log_info "正在获取 K8s 最新稳定版..."
        K8S_VERSION=$(curl -sSL https://dl.k8s.io/release/stable.txt 2>/dev/null | sed 's/v//' || echo "1.35.1")
        log_info "K8s 版本: v$K8S_VERSION"
    fi
    
    # 获取 KubeEdge 最新稳定版
    if [[ -z "$KUBEEDGE_VERSION" ]]; then
        log_info "正在获取 KubeEdge 最新稳定版..."
        KUBEEDGE_VERSION=$(curl -sSL https://api.github.com/repos/kubeedge/kubeedge/releases/latest 2>/dev/null | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/' || echo "1.22.1")
        log_info "KubeEdge 版本: v$KUBEEDGE_VERSION"
    fi
}

#===============================================================================
# 系统初始化
#===============================================================================
init_system() {
    log_step "系统初始化配置"
    
    # 关闭 swap
    log_info "关闭 swap..."
    swapoff -a
    sed -i '/swap/s/^/#/' /etc/fstab
    
    # 加载必要内核模块
    log_info "加载内核模块..."
    cat > /etc/modules-load.d/k8s.conf << EOF
overlay
br_netfilter
EOF
    modprobe overlay
    modprobe br_netfilter
    
    # 设置内核参数
    log_info "配置内核参数..."
    cat > /etc/sysctl.d/k8s.conf << EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
vm.swappiness                       = 0
EOF
    sysctl --system > /dev/null 2>&1
    
    # 关闭防火墙
    log_info "配置防火墙..."
    if systemctl is-active --quiet ufw; then
        systemctl stop ufw
        systemctl disable ufw
    fi
    
    # Ubuntu 24.04: 放行 AppArmor 非特权用户命名空间限制 (containerd 需要)
    if [[ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]]; then
        CURRENT_VAL=$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo "0")
        if [[ "$CURRENT_VAL" != "0" ]]; then
            log_info "放行 AppArmor 非特权用户命名空间限制..."
            echo 0 > /proc/sys/kernel/apparmor_restrict_unprivileged_userns
            if ! grep -q 'apparmor_restrict_unprivileged_userns' /etc/sysctl.d/k8s.conf 2>/dev/null; then
                echo 'kernel.apparmor_restrict_unprivileged_userns = 0' >> /etc/sysctl.d/k8s.conf
                sysctl --system > /dev/null 2>&1
            fi
        fi
    fi
    
    # 设置时区
    timedatectl set-timezone Asia/Shanghai 2>/dev/null || true
    
    log_info "系统初始化完成"
}

#===============================================================================
# 安装容器运行时 (containerd)
#===============================================================================
install_containerd() {
    log_step "安装容器运行时 containerd"
    
    local NEED_INSTALL=false
    local NEED_CONFIG=false
    
    # 检查是否已安装
    if ! command -v containerd &>/dev/null; then
        log_info "containerd 未安装，开始安装..."
        NEED_INSTALL=true
    else
        log_info "containerd 已安装: $(containerd --version)"
    fi
    
    # 如果需要安装
    if [[ "$NEED_INSTALL" == "true" ]]; then
        # 安装依赖
        log_info "安装依赖包..."
        apt-get update -qq
        apt-get install -y -qq ca-certificates curl gnupg lsb-release apt-transport-https \
            socat conntrack ebtables ipset
        
        # 添加 Docker 官方 GPG key
        log_info "添加 Docker GPG key..."
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
        chmod a+r /etc/apt/keyrings/docker.gpg
        
        # 添加仓库
        log_info "添加 Docker 仓库..."
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
        
        # 安装 containerd
        log_info "安装 containerd..."
        apt-get update -qq
        apt-get install -y -qq containerd.io
        
        NEED_CONFIG=true
    fi
    
    # 检查配置文件是否存在
    if [[ ! -f /etc/containerd/config.toml ]]; then
        log_info "containerd 配置文件不存在，生成默认配置..."
        NEED_CONFIG=true
    fi
    
    # 配置 containerd
    if [[ "$NEED_CONFIG" == "true" ]]; then
        log_info "配置 containerd..."
        mkdir -p /etc/containerd
        containerd config default > /etc/containerd/config.toml
        
        # 启用 SystemdCgroup
        sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
        
        # 确保 CRI 插件未被禁用
        sed -i 's/disabled_plugins.*=.*\[.*"cri".*\]/disabled_plugins = []/' /etc/containerd/config.toml
        
        # 配置国内镜像加速
        if [[ "$USE_CN_MIRROR" == "true" ]]; then
            log_info "配置镜像加速..."
            
            # 检测 containerd 主版本 (2.x 使用 hosts.toml，1.x 使用旧格式)
            CONTAINERD_MAJOR=$(containerd --version 2>/dev/null | grep -oP 'containerd\.io \K\d+' || echo "1")
            
            if [[ "$CONTAINERD_MAJOR" -ge 2 ]]; then
                # ── containerd 2.x: hosts.toml 格式 ──
                log_info "检测到 containerd 2.x，使用 hosts.toml 镜像配置..."
                
                # 确保 config_path 已在 config.toml 中启用
                if ! grep -q 'config_path.*certs.d' /etc/containerd/config.toml; then
                    sed -i '/\[plugins.*registry\]/a\      config_path = "/etc/containerd/certs.d"' /etc/containerd/config.toml 2>/dev/null || true
                fi
                
                # K8s 组件镜像加速
                mkdir -p /etc/containerd/certs.d/registry.k8s.io
                cat > /etc/containerd/certs.d/registry.k8s.io/hosts.toml << 'TOML'
server = "https://registry.k8s.io"

[host."https://registry.aliyuncs.com/google_containers"]
  capabilities = ["pull", "resolve"]
TOML
                
                # Docker Hub 镜像加速 (Calico 等组件需要)
                mkdir -p /etc/containerd/certs.d/docker.io
                cat > /etc/containerd/certs.d/docker.io/hosts.toml << 'TOML'
server = "https://docker.io"

[host."https://docker.m.daocloud.io"]
  capabilities = ["pull", "resolve"]
TOML
            else
                # ── containerd 1.x: 旧格式 ──
                # K8s 组件镜像加速
                sed -i 's|registry.k8s.io|registry.aliyuncs.com/google_containers|g' /etc/containerd/config.toml
                
                # Docker Hub 镜像加速 (Calico 等组件需要)
                if ! grep -q 'registry.mirrors.*docker.io' /etc/containerd/config.toml; then
                    log_info "配置 Docker Hub 镜像加速..."
                    cat >> /etc/containerd/config.toml << 'EOF'

# Docker Hub 镜像加速 - DaoCloud
[plugins."io.containerd.grpc.v1.cri".registry.mirrors."docker.io"]
  endpoint = ["https://docker.m.daocloud.io"]
EOF
                else
                    log_info "Docker Hub 镜像加速已配置"
                fi
            fi
        fi
    fi
    
    # 确保服务启动
    log_info "启动 containerd 服务..."
    systemctl daemon-reload
    systemctl enable containerd
    systemctl restart containerd
    
    # 等待 socket 就绪
    log_info "等待 containerd socket 就绪..."
    for i in {1..10}; do
        if [[ -S /run/containerd/containerd.sock ]]; then
            log_info "containerd socket 已就绪"
            break
        fi
        log_info "等待 containerd 启动... ($i/10)"
        sleep 2
    done
    
    # 最终检查
    if [[ ! -S /run/containerd/containerd.sock ]]; then
        log_error "containerd socket 不存在，请检查服务状态: systemctl status containerd"
        exit 1
    fi
    
    log_info "containerd 安装完成: $(containerd --version)"
}

#===============================================================================
# 安装 K8s 组件
#===============================================================================
install_k8s_components() {
    log_step "安装 Kubernetes 组件"
    
    # 修复无法访问的apt源
    if grep -q 'mirrors.ivolces.com' /etc/apt/sources.list 2>/dev/null; then
        log_info "修复 apt 源..."
        sed -i 's|mirrors.ivolces.com|mirrors.aliyun.com|g' /etc/apt/sources.list
    fi
    
    # 添加 K8s 仓库
    log_info "添加 Kubernetes 仓库..."
    
    # 使用新的 K8s 仓库地址
    K8S_MINOR_VERSION=$(echo $K8S_VERSION | cut -d. -f1,2)
    
    curl -fsSL "https://pkgs.k8s.io/core:/stable:/v${K8S_MINOR_VERSION}/deb/Release.key" | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg 2>/dev/null
    echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v${K8S_MINOR_VERSION}/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
    
    # 安装 kubeadm, kubelet, kubectl
    log_info "安装 kubeadm, kubelet, kubectl..."
    apt-get update -qq
    apt-get install -y -qq kubelet kubeadm kubectl
    apt-mark hold kubelet kubeadm kubectl
    
    log_info "Kubernetes 组件安装完成"
    log_info "  kubectl: $(kubectl version --client --short 2>/dev/null || kubectl version --client)"
    log_info "  kubeadm: $(kubeadm version -o short 2>/dev/null || kubeadm version)"
}

#===============================================================================
# 初始化 K8s 集群
#===============================================================================
init_k8s_cluster() {
    log_step "初始化 Kubernetes 集群"
    
    # 检查是否已经初始化
    if [[ -f /etc/kubernetes/admin.conf ]]; then
        log_warn "K8s 集群已初始化，跳过..."
        return 0
    fi
    
    # 预拉取镜像
    log_info "预拉取 K8s 镜像..."
    if [[ "$USE_CN_MIRROR" == "true" ]]; then
        kubeadm config images pull --image-repository registry.aliyuncs.com/google_containers 2>&1 | grep -E "^(Pulled|Pulling)" || true
    else
        kubeadm config images pull 2>&1 | grep -E "^(Pulled|Pulling)" || true
    fi
    
    # 初始化集群
    log_info "初始化 K8s 集群..."
    INIT_OPTS="--pod-network-cidr=$POD_CIDR --service-cidr=$SERVICE_CIDR --cri-socket=unix:///run/containerd/containerd.sock"
    
    # 添加公网IP到证书SAN（支持外部访问）
    if [[ -n "$CLOUD_PUBLIC_IP" ]]; then
        INIT_OPTS="$INIT_OPTS --apiserver-cert-extra-sans=$CLOUD_PUBLIC_IP"
        log_info "API Server 证书将包含公网IP: $CLOUD_PUBLIC_IP"
    fi
    
    if [[ "$USE_CN_MIRROR" == "true" ]]; then
        INIT_OPTS="$INIT_OPTS --image-repository registry.aliyuncs.com/google_containers"
    fi
    
    kubeadm init $INIT_OPTS 2>&1 | tee /tmp/kubeadm-init.log
    
    # 配置 kubectl
    log_info "配置 kubectl..."
    mkdir -p $HOME/.kube
    cp -f /etc/kubernetes/admin.conf $HOME/.kube/config
    chown $(id -u):$(id -g) $HOME/.kube/config
    export KUBECONFIG=$KUBE_CONFIG
    
    # 允许 master 节点调度 Pod (单节点集群)
    log_info "配置 master 节点可调度..."
    kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>/dev/null || true
    
    # 配置 kube-proxy 禁止在边缘节点运行
    log_info "配置 kube-proxy 排除边缘节点..."
    kubectl -n kube-system patch daemonset kube-proxy --type merge -p '
    {"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"node-role.kubernetes.io/edge","operator":"DoesNotExist"}]}]}}}}}}}' 2>/dev/null || true
    
    log_info "K8s 集群初始化完成"
}

#===============================================================================
# 安装网络插件 (Calico)
#===============================================================================
install_network_plugin() {
    log_step "安装网络插件 Calico"
    
    # 检查是否已安装
    if kubectl get pods -n kube-system 2>/dev/null | grep -q "calico.*Running"; then
        log_info "Calico 已安装"
        return 0
    fi
    
    log_info "下载 Calico 配置文件..."
    
    # 下载 Calico manifest
    CALICO_VERSION="v3.29.2"
    CALICO_FILE="calico.yaml"
    CALICO_URL="https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    
    cd /tmp
    
    # 优先检查本地文件（当前目录、cache目录、/tmp）
    if [[ -f "$SCRIPT_DIR/$CALICO_FILE" ]]; then
        log_info "使用本地文件: $SCRIPT_DIR/$CALICO_FILE"
        cp "$SCRIPT_DIR/$CALICO_FILE" /tmp/
    elif [[ -f "$SCRIPT_DIR/cache/$CALICO_FILE" ]]; then
        log_info "使用本地文件: $SCRIPT_DIR/cache/$CALICO_FILE"
        cp "$SCRIPT_DIR/cache/$CALICO_FILE" /tmp/
    elif [[ -f "/tmp/$CALICO_FILE" ]]; then
        log_info "使用已下载文件: /tmp/$CALICO_FILE"
    else
        log_info "从 GitHub 下载..."
        curl -LO "$CALICO_URL" 2>/dev/null || \
            curl -LO "https://docs.projectcalico.org/manifests/calico.yaml" || {
                log_error "Calico 配置文件下载失败，请手动下载并放置到脚本同目录"
                exit 1
            }
        # 下载成功后复制一份到脚本目录
        cp "/tmp/$CALICO_FILE" "$SCRIPT_DIR/" 2>/dev/null || true
    fi
    
    # 修改 CIDR 配置 (默认 192.168.0.0/16 改为 $POD_CIDR)
    if [[ "$POD_CIDR" != "192.168.0.0/16" ]]; then
        log_info "配置 Pod CIDR: $POD_CIDR"
        sed -i "s|192.168.0.0/16|$POD_CIDR|g" calico.yaml
        # 取消注释 CALICO_IPV4POOL_CIDR
        sed -i 's/# - name: CALICO_IPV4POOL_CIDR/- name: CALICO_IPV4POOL_CIDR/' calico.yaml
        sed -i "s|#   value: \"192.168.0.0/16\"|  value: \"$POD_CIDR\"|" calico.yaml
    fi
    
    log_info "安装 Calico CNI..."
    kubectl apply -f calico.yaml
    
    # 清理下载的文件
    rm -f calico.yaml
    
    # 等待 Calico 就绪
    log_info "等待 Calico 就绪..."
    for i in {1..60}; do
        if kubectl get pods -n kube-system 2>/dev/null | grep -E "calico-node.*Running" | grep -v "0/"; then
            log_info "Calico 启动成功"
            break
        fi
        log_info "等待 Calico 启动... ($i/60)"
        sleep 5
    done
    
    log_info "Calico 安装完成"
}

#===============================================================================
# 安装 keadm
#===============================================================================
install_keadm() {
    log_step "安装 keadm"
    
    if command -v keadm &>/dev/null; then
        CURRENT_VERSION=$(keadm version 2>/dev/null | grep -oP 'v\d+\.\d+\.\d+' | head -1 | sed 's/v//')
        if [[ "$CURRENT_VERSION" == "$KUBEEDGE_VERSION" ]]; then
            log_info "keadm v$KUBEEDGE_VERSION 已安装"
            return 0
        fi
    fi
    
    log_info "下载 keadm v$KUBEEDGE_VERSION..."
    ARCH=$(uname -m)
    case $ARCH in
        x86_64) ARCH="amd64" ;;
        aarch64) ARCH="arm64" ;;
    esac
    
    KEADM_FILE="keadm-v${KUBEEDGE_VERSION}-linux-${ARCH}.tar.gz"
    KEADM_URL="https://github.com/kubeedge/kubeedge/releases/download/v${KUBEEDGE_VERSION}/${KEADM_FILE}"
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    
    cd /tmp
    
    # 优先检查本地文件（当前目录、cache目录、/tmp）
    if [[ -f "$SCRIPT_DIR/$KEADM_FILE" ]]; then
        log_info "使用本地文件: $SCRIPT_DIR/$KEADM_FILE"
        cp "$SCRIPT_DIR/$KEADM_FILE" /tmp/
    elif [[ -f "$SCRIPT_DIR/cache/$KEADM_FILE" ]]; then
        log_info "使用本地文件: $SCRIPT_DIR/cache/$KEADM_FILE"
        cp "$SCRIPT_DIR/cache/$KEADM_FILE" /tmp/
    elif [[ -f "/tmp/$KEADM_FILE" ]]; then
        log_info "使用已下载文件: /tmp/$KEADM_FILE"
    else
        log_info "从 GitHub 下载..."
        curl -LO "$KEADM_URL" || {
            log_error "GitHub 下载失败，尝试使用代理..."
            curl -LO "https://ghproxy.com/$KEADM_URL" || {
                log_error "keadm 下载失败，请手动下载并放置到脚本同目录"
                exit 1
            }
        }
        # 下载成功后复制一份到脚本目录
        cp "/tmp/$KEADM_FILE" "$SCRIPT_DIR/" 2>/dev/null || true
    fi
    
    tar -xzf "$KEADM_FILE"
    cp keadm-v${KUBEEDGE_VERSION}-linux-${ARCH}/keadm/keadm /usr/local/bin/
    chmod +x /usr/local/bin/keadm
    rm -rf keadm-v${KUBEEDGE_VERSION}-linux-${ARCH}/
    rm -f "$KEADM_FILE"  # 删除/tmp中的文件，脚本目录已保留
    
    log_info "keadm 安装完成: $(keadm version)"
}

#===============================================================================
# 安装 CloudCore
#===============================================================================
install_cloudcore() {
    log_step "安装 KubeEdge CloudCore"
    
    if [[ -z "$CLOUD_PUBLIC_IP" ]]; then
        log_error "请使用 --cloud-ip 参数指定云主机公网 IP"
        exit 1
    fi
    
    # 检查 kubeconfig 是否存在
    if [[ ! -f "$KUBE_CONFIG" ]]; then
        if [[ -f /etc/kubernetes/admin.conf ]]; then
            log_info "配置 kubeconfig..."
            mkdir -p $(dirname "$KUBE_CONFIG")
            cp -f /etc/kubernetes/admin.conf "$KUBE_CONFIG"
            chown $(id -u):$(id -g) "$KUBE_CONFIG"
        else
            log_error "kubeconfig 不存在，请先完成 K8s 集群初始化"
            exit 1
        fi
    fi
    export KUBECONFIG="$KUBE_CONFIG"
    
    # 检查是否已安装
    if kubectl get pods -n kubeedge 2>/dev/null | grep -q "cloudcore.*Running"; then
        log_info "CloudCore 已安装并运行"
        # 仍然需要配置 stream
        configure_cloudstream
        return 0
    fi
    
    log_info "初始化 CloudCore..."
    log_info "  公网IP: $CLOUD_PUBLIC_IP"
    log_info "  WebSocket端口: $CLOUDCORE_PORT"
    log_info "  QUIC端口: $CLOUDCORE_QUIC_PORT"
    log_info "  HTTPS端口: $CLOUDCORE_HTTPS_PORT"
    log_info "  Stream端口: $CLOUDCORE_STREAM_PORT"
    
    # 初始化 CloudCore
    keadm init --advertise-address="$CLOUD_PUBLIC_IP" \
               --kubeedge-version="$KUBEEDGE_VERSION" \
               --kube-config=$KUBE_CONFIG \
               --set cloudCore.modules.dynamicController.enable=true \
               --force
    
    # 等待 CloudCore 就绪
    log_info "等待 CloudCore 就绪..."
    sleep 10
    
    for i in {1..30}; do
        if kubectl get pods -n kubeedge 2>/dev/null | grep -q "cloudcore.*Running"; then
            log_info "CloudCore 启动成功"
            break
        fi
        log_info "等待 CloudCore 启动... ($i/30)"
        sleep 5
    done
    
    # 配置 CloudStream (支持 kubectl exec/logs)
    configure_cloudstream
    
    # 开放防火墙端口
    log_info "配置防火墙规则..."
    if command -v iptables &>/dev/null; then
        iptables -A INPUT -p tcp --dport $CLOUDCORE_PORT -j ACCEPT 2>/dev/null || true
        iptables -A INPUT -p tcp --dport $CLOUDCORE_QUIC_PORT -j ACCEPT 2>/dev/null || true
        iptables -A INPUT -p tcp --dport $CLOUDCORE_HTTPS_PORT -j ACCEPT 2>/dev/null || true
        iptables -A INPUT -p tcp --dport $CLOUDCORE_STREAM_PORT -j ACCEPT 2>/dev/null || true
        iptables -A INPUT -p tcp --dport $CLOUDCORE_TUNNEL_PORT -j ACCEPT 2>/dev/null || true
        iptables -A INPUT -p tcp --dport 6443 -j ACCEPT 2>/dev/null || true
    fi
    
    log_info "CloudCore 安装完成"
}

#===============================================================================
# 配置 CloudStream (支持 kubectl exec/logs)
#===============================================================================
configure_cloudstream() {
    log_step "配置 CloudStream (kubectl exec/logs 支持)"
    
    CLOUDCORE_CONFIG="/etc/kubeedge/config/cloudcore.yaml"
    
    if [[ ! -f "$CLOUDCORE_CONFIG" ]]; then
        log_warn "CloudCore 配置文件不存在，跳过 Stream 配置"
        return 0
    fi
    
    # 备份配置
    cp "$CLOUDCORE_CONFIG" "${CLOUDCORE_CONFIG}.bak"
    
    # 启用 cloudStream
    log_info "启用 cloudStream..."
    
    # 使用 sed 修改配置
    # 启用 cloudStream.enable
    sed -i 's/enable: false/enable: true/g' "$CLOUDCORE_CONFIG"
    
    # 确保 cloudStream 配置正确
    if ! grep -q "cloudStream:" "$CLOUDCORE_CONFIG"; then
        log_warn "配置文件格式可能不兼容，尝试手动添加 cloudStream 配置"
    fi
    
    # 配置 iptables 规则，将 10350 端口流量转发到 cloudcore
    log_info "配置 iptables 转发规则..."
    
    # 获取 cloudcore pod IP
    CLOUDCORE_POD_IP=$(kubectl get pods -n kubeedge -l k8s-app=cloudcore -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)
    
    if [[ -n "$CLOUDCORE_POD_IP" ]]; then
        log_info "CloudCore Pod IP: $CLOUDCORE_POD_IP"
        
        # 清理旧规则
        iptables -t nat -D OUTPUT -p tcp --dport 10350 -j DNAT --to $CLOUDCORE_POD_IP:10003 2>/dev/null || true
        
        # 添加新规则：将 API Server 对 10350 的请求转发到 CloudCore 的 10003 端口
        iptables -t nat -A OUTPUT -p tcp --dport 10350 -j DNAT --to $CLOUDCORE_POD_IP:10003
        
        log_info "iptables 转发规则配置完成"
    else
        log_warn "无法获取 CloudCore Pod IP，请手动配置 iptables"
        log_warn "手动执行: iptables -t nat -A OUTPUT -p tcp --dport 10350 -j DNAT --to <CLOUDCORE_POD_IP>:10003"
    fi
    
    # 重启 CloudCore
    log_info "重启 CloudCore 以应用配置..."
    kubectl delete pod -n kubeedge -l k8s-app=cloudcore 2>/dev/null || true
    
    # 等待重启完成
    sleep 5
    for i in {1..30}; do
        if kubectl get pods -n kubeedge 2>/dev/null | grep -q "cloudcore.*Running"; then
            log_info "CloudCore 重启成功"
            
            # 重新获取 Pod IP 并更新 iptables
            sleep 3
            NEW_POD_IP=$(kubectl get pods -n kubeedge -l k8s-app=cloudcore -o jsonpath='{.items[0].status.podIP}' 2>/dev/null || true)
            if [[ -n "$NEW_POD_IP" ]] && [[ "$NEW_POD_IP" != "$CLOUDCORE_POD_IP" ]]; then
                log_info "更新 iptables 规则，新 Pod IP: $NEW_POD_IP"
                iptables -t nat -D OUTPUT -p tcp --dport 10350 -j DNAT --to $CLOUDCORE_POD_IP:10003 2>/dev/null || true
                iptables -t nat -A OUTPUT -p tcp --dport 10350 -j DNAT --to $NEW_POD_IP:10003
            fi
            break
        fi
        log_info "等待 CloudCore 重启... ($i/30)"
        sleep 3
    done
    
    log_info "CloudStream 配置完成"
    log_info "现在可以使用 kubectl exec/logs 访问边缘节点 Pod"
}

#===============================================================================
# 生成边缘节点加入 Token
#===============================================================================
generate_token() {
    log_step "生成边缘节点加入 Token"
    
    # 尝试自动获取公网IP（可选）
    if [[ -z "$CLOUD_PUBLIC_IP" ]]; then
        # 从本机网络接口获取（排除内网IP）
        CLOUD_PUBLIC_IP=$(ip addr | grep 'inet ' | grep -v '127.0.0.1' | grep -v '192.168' | grep -v '10\.' | grep -v '172\.' | awk '{print $2}' | cut -d/ -f1 | head -1 2>/dev/null || true)
    fi
    
    log_info "生成加入 Token..."
    TOKEN=$(keadm gettoken --kube-config=$KUBE_CONFIG)
    
    echo ""
    echo "============================================================"
    echo -e "${GREEN}边缘节点加入信息${NC}"
    echo "============================================================"
    echo ""
    echo -e "${YELLOW}Token:${NC}"
    echo "$TOKEN"
    echo ""
    
    # 根据是否有公网IP显示不同提示
    if [[ -n "$CLOUD_PUBLIC_IP" ]]; then
        echo -e "${YELLOW}在边缘节点执行以下命令加入集群:${NC}"
        echo ""
        echo "# 方式1: 使用 edge-join.sh 脚本 (推荐)"
        echo "./edge-join.sh join --cloud-ip $CLOUD_PUBLIC_IP --token $TOKEN"
        echo ""
        echo "# 方式2: 直接使用 keadm 命令"
        echo "keadm join --cloudcore-ipport=$CLOUD_PUBLIC_IP:10000 --token=$TOKEN --kubeedge-version=$KUBEEDGE_VERSION --cgroupdriver=systemd --remote-runtime-endpoint=unix:///var/run/cri-dockerd.sock"
    else
        log_warn "未检测到公网IP，请将下方命令中的 <CLOUD_IP> 替换为实际公网IP"
        echo ""
        echo -e "${YELLOW}在边缘节点执行以下命令加入集群:${NC}"
        echo ""
        echo "# 方式1: 使用 edge-join.sh 脚本 (推荐)"
        echo "./edge-join.sh join --cloud-ip <CLOUD_IP> --token $TOKEN"
        echo ""
        echo "# 方式2: 直接使用 keadm 命令"
        echo "keadm join --cloudcore-ipport=<CLOUD_IP>:10000 --token=$TOKEN --kubeedge-version=$KUBEEDGE_VERSION --cgroupdriver=systemd --remote-runtime-endpoint=unix:///var/run/cri-dockerd.sock"
    fi
    echo ""
    echo "============================================================"
    echo ""
    
    # 保存 Token 到文件
    mkdir -p /etc/kubeedge
    echo "$TOKEN" > /etc/kubeedge/edge-token
    log_info "Token 已保存到 /etc/kubeedge/edge-token"
}

#===============================================================================
# 查看集群状态
#===============================================================================
show_status() {
    log_step "集群状态"
    
    echo ""
    echo "=== K8s 集群信息 ==="
    kubectl cluster-info 2>/dev/null || echo "K8s 集群未初始化"
    
    echo ""
    echo "=== 节点状态 ==="
    kubectl get nodes -o wide 2>/dev/null || echo "无法获取节点信息"
    
    echo ""
    echo "=== 系统 Pod 状态 ==="
    kubectl get pods -n kube-system 2>/dev/null || echo "无法获取 Pod 信息"
    
    echo ""
    echo "=== KubeEdge 状态 ==="
    kubectl get pods -n kubeedge 2>/dev/null || echo "KubeEdge 未安装"
    
    echo ""
    echo "=== 边缘节点 ==="
    kubectl get nodes -l node-role.kubernetes.io/edge='' 2>/dev/null || echo "无边缘节点"
    
    echo ""
}

#===============================================================================
# 重置集群
#===============================================================================
reset_cluster() {
    log_step "重置集群"
    
    read -p "确定要重置集群吗？这将删除所有数据！(y/N): " confirm
    if [[ ! "$confirm" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        log_info "操作取消"
        exit 0
    fi
    
    # 重置 KubeEdge
    log_info "重置 KubeEdge..."
    keadm reset cloud --kube-config=$KUBE_CONFIG --force 2>/dev/null || true
    
    # 停止服务并杀死残留进程
    log_info "停止服务..."
    systemctl stop kubelet 2>/dev/null || true
    systemctl stop containerd 2>/dev/null || true
    
    # 杀死占用6443端口的进程
    if lsof -i :6443 &>/dev/null; then
        log_info "清理占用6443端口..."
        kill -9 $(lsof -t -i :6443) 2>/dev/null || true
    fi
    
    # 重置 K8s
    log_info "重置 Kubernetes..."
    kubeadm reset -f 2>/dev/null || true
    
    # 清理网络
    log_info "清理网络配置..."
    ip link delete cni0 2>/dev/null || true
    ip link delete tunl0 2>/dev/null || true
    ip link delete vxlan.calico 2>/dev/null || true
    rm -rf /etc/cni/net.d/*
    
    # 清理 iptables
    iptables -F && iptables -t nat -F && iptables -t mangle -F && iptables -X 2>/dev/null || true
    
    # 清理配置文件
    rm -rf $HOME/.kube/config
    rm -rf /etc/kubernetes/*
    rm -rf /etc/kubeedge/*
    rm -rf /var/lib/kubelet/*
    rm -rf /var/lib/etcd/*
    
    log_info "集群重置完成"
}

#===============================================================================
# 完全卸载
#===============================================================================
uninstall_all() {
    log_step "完全卸载"
    
    read -p "确定要完全卸载 K8s 和 KubeEdge 吗？(y/N): " confirm
    if [[ ! "$confirm" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        log_info "操作取消"
        exit 0
    fi
    
    # 1. 停止服务
    log_info "停止服务..."
    systemctl stop kubelet 2>/dev/null || true
    systemctl stop containerd 2>/dev/null || true
    systemctl disable kubelet 2>/dev/null || true
    systemctl disable containerd 2>/dev/null || true
    
    # 2. 重置 KubeEdge
    log_info "重置 KubeEdge..."
    keadm reset cloud --kube-config=$KUBE_CONFIG --force 2>/dev/null || true
    
    # 3. 重置 K8s
    log_info "重置 Kubernetes..."
    kubeadm reset -f 2>/dev/null || true
    
    # 4. 卸载 K8s 组件
    log_info "卸载 Kubernetes 组件..."
    apt-mark unhold kubelet kubeadm kubectl 2>/dev/null || true
    apt-get purge -y kubelet kubeadm kubectl 2>/dev/null || true
    
    # 5. 卸载 containerd
    log_info "卸载 containerd..."
    apt-get purge -y containerd.io 2>/dev/null || true
    
    # 6. 删除 keadm
    log_info "删除 keadm..."
    rm -f /usr/local/bin/keadm
    
    # 7. 清理配置文件
    log_info "清理配置文件..."
    rm -rf $HOME/.kube
    rm -rf /etc/kubernetes
    rm -rf /etc/kubeedge
    rm -rf /var/lib/kubelet
    rm -rf /var/lib/etcd
    rm -rf /var/lib/containerd
    rm -rf /etc/cni
    rm -rf /opt/cni
    rm -rf /etc/apt/sources.list.d/kubernetes.list
    rm -rf /etc/apt/keyrings/kubernetes-apt-keyring.gpg
    
    # 8. 清理网络
    log_info "清理网络配置..."
    ip link delete cni0 2>/dev/null || true
    ip link delete tunl0 2>/dev/null || true
    ip link delete vxlan.calico 2>/dev/null || true
    
    # 9. 清理 iptables (含 CloudStream 转发规则)
    log_info "清理 iptables 规则..."
    iptables -t nat -D OUTPUT -p tcp --dport 10350 -j DNAT --to-destination 10.244.0.0/16:10003 2>/dev/null || true
    iptables -F && iptables -t nat -F && iptables -t mangle -F && iptables -X 2>/dev/null || true
    
    # 10. 清理残留包
    apt-get autoremove -y 2>/dev/null || true
    
    log_info "卸载完成"
    log_info "日志文件: $LOG_FILE"
}

#===============================================================================
# 完整安装
#===============================================================================
full_install() {
    log_step "开始完整安装 K8s + KubeEdge"
    
    if [[ -z "$CLOUD_PUBLIC_IP" ]]; then
        log_error "请使用 --cloud-ip 参数指定云主机公网 IP"
        log_error "示例: $0 install --cloud-ip 1.2.3.4"
        exit 1
    fi
    
    START_TIME=$(date +%s)
    
    check_system
    check_public_ip
    get_latest_versions
    init_system
    install_containerd
    install_k8s_components
    init_k8s_cluster
    install_network_plugin
    install_keadm
    install_cloudcore
    
    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))
    
    echo ""
    log_step "安装完成"
    echo ""
    echo "============================================================"
    echo -e "${GREEN}K8s + KubeEdge 安装成功!${NC}"
    echo "============================================================"
    echo ""
    echo "  K8s 版本:      v$K8S_VERSION"
    echo "  KubeEdge 版本: v$KUBEEDGE_VERSION"
    echo "  云主机 IP:     $CLOUD_PUBLIC_IP"
    echo "  安装耗时:      ${DURATION}秒"
    echo ""
    echo "============================================================"
    echo ""
    
    # 显示节点状态
    kubectl get nodes -o wide
    
    # 直接生成并显示Token
    generate_token
}

#===============================================================================
# 仅安装 K8s
#===============================================================================
install_k8s_only() {
    log_step "开始安装 Kubernetes"
    
    check_system
    get_latest_versions
    init_system
    install_containerd
    install_k8s_components
    init_k8s_cluster
    install_network_plugin
    
    log_info "Kubernetes 安装完成"
    kubectl get nodes -o wide
}

#===============================================================================
# 仅安装 KubeEdge
#===============================================================================
install_kubeedge_only() {
    log_step "开始安装 KubeEdge"
    
    if [[ -z "$CLOUD_PUBLIC_IP" ]]; then
        log_error "请使用 --cloud-ip 参数指定云主机公网 IP"
        exit 1
    fi
    
    if [[ ! -f /etc/kubernetes/admin.conf ]]; then
        log_error "K8s 集群未初始化，请先执行 install-k8s"
        exit 1
    fi
    
    check_public_ip
    get_latest_versions
    install_keadm
    install_cloudcore
    
    log_info "KubeEdge 安装完成"
    generate_token
}

#===============================================================================
# 主入口
#===============================================================================
main() {
    parse_args "$@"
    
    # 权限检查 (help/status不需要root)
    if [[ "$COMMAND" != "help" && "$COMMAND" != "status" && $EUID -ne 0 ]]; then
        echo -e "${RED}[ERROR]${NC} 此脚本需要 root 权限执行"
        echo "请使用: sudo $0 $COMMAND ..."
        exit 1
    fi
    
    case $COMMAND in
        install)
            full_install
            ;;
        install-k8s)
            install_k8s_only
            ;;
        install-kubeedge)
            install_kubeedge_only
            ;;
        uninstall)
            uninstall_all
            ;;
        reset)
            reset_cluster
            ;;
        status)
            show_status
            ;;
        token)
            generate_token
            ;;
        help)
            show_help
            ;;
        *)
            show_help
            exit 1
            ;;
    esac
}

main "$@"
