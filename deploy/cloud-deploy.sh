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
KUBEEDGE_VERSION="1.22.1"
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
    
    # 公网IP未在本机，判断是否为云主机 NAT 场景
    echo ""
    echo "============================================================"
    echo -e "${YELLOW}公网IP $CLOUD_PUBLIC_IP 未配置到本机网络接口${NC}"
    echo "============================================================"
    echo ""
    echo "如果这是云主机 (EIP/NAT 映射)，无需配置，CloudCore 绑定 0.0.0.0 即可。"
    echo "如果这是物理机/内网服务器，可能需要将公网IP配置到本机。"
    echo ""
    echo "  1) 配置到子接口 - 添加到 eth0:1 虚拟接口"
    echo "  2) 跳过 - 云主机 NAT 场景"
    echo ""
    read -p "请选择 [1/2] (默认 1): " choice
    choice=${choice:-1}
    
    if [[ "$choice" == "2" ]]; then
        log_info "跳过公网IP本地配置 (云主机 NAT 模式)"
        log_info "CloudCore 将绑定 0.0.0.0，边缘节点通过公网IP $CLOUD_PUBLIC_IP 连接"
    else
        configure_public_ip_secondary
    fi
}

#===============================================================================
# 将公网IP配置到子接口 (物理机/特殊网络场景)
#===============================================================================
configure_public_ip_secondary() {
    log_info "将公网IP配置到子接口..."
    
    # 获取默认网络接口
    DEFAULT_IFACE=$(ip route | grep default | awk '{print $5}' | head -1)
    if [[ -z "$DEFAULT_IFACE" ]]; then
        DEFAULT_IFACE="eth0"
    fi
    
    log_info "主接口: $DEFAULT_IFACE"
    log_info "添加公网IP: $CLOUD_PUBLIC_IP/32 到 ${DEFAULT_IFACE}:1"
    
    # 立即生效: ip addr add
    if ip addr show $DEFAULT_IFACE | grep -q "$CLOUD_PUBLIC_IP"; then
        log_info "公网IP已存在于 $DEFAULT_IFACE"
    else
        ip addr add ${CLOUD_PUBLIC_IP}/32 dev ${DEFAULT_IFACE} label ${DEFAULT_IFACE}:1 || {
            log_error "IP添加失败"
            return 1
        }
    fi
    
    # 持久化: 写入 systemd 服务 (不用 netplan，避免冲突)
    local SERVICE_FILE="/etc/systemd/system/kubeedge-public-ip.service"
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Add public IP for KubeEdge CloudCore
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/sbin/ip addr add ${CLOUD_PUBLIC_IP}/32 dev ${DEFAULT_IFACE} label ${DEFAULT_IFACE}:1
ExecStart=-/bin/true
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable kubeedge-public-ip.service 2>/dev/null
    
    # 清理可能存在的错误 netplan 配置
    rm -f /etc/netplan/60-kubeedge-public-ip.yaml 2>/dev/null
    
    # 验证
    if ip addr show $DEFAULT_IFACE | grep -q "$CLOUD_PUBLIC_IP"; then
        log_info "公网IP配置成功: ${DEFAULT_IFACE}:1 -> $CLOUD_PUBLIC_IP"
    else
        log_warn "公网IP配置可能未生效，请手动检查: ip addr show $DEFAULT_IFACE"
    fi
}

#===============================================================================
# 获取最新版本
#===============================================================================
get_k8s_version() {
    if [[ -z "$K8S_VERSION" ]]; then
        log_info "正在获取 K8s 最新稳定版..."
        K8S_VERSION=$(curl -sSL https://dl.k8s.io/release/stable.txt 2>/dev/null | sed 's/v//' || echo "1.35.1")
        log_info "K8s 版本: v$K8S_VERSION"
    fi
}

get_kubeedge_version() {
    if [[ -z "$KUBEEDGE_VERSION" ]]; then
        log_info "正在获取 KubeEdge 最新稳定版..."
        KUBEEDGE_VERSION=$(curl -sSL https://api.github.com/repos/kubeedge/kubeedge/releases/latest 2>/dev/null | grep '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/' || echo "1.22.1")
        log_info "KubeEdge 版本: v$KUBEEDGE_VERSION"
    fi
}

get_latest_versions() {
    log_step "获取最新稳定版本"
    get_k8s_version
    get_kubeedge_version
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
    
    # 检查是否已安装 containerd.io (Docker 官方包)
    if ! command -v containerd &>/dev/null; then
        log_info "containerd 未安装，开始安装..."
        NEED_INSTALL=true
    elif ! dpkg -l containerd.io &>/dev/null 2>&1; then
        # 系统自带的 containerd 包 (非 Docker 官方)，版本可能太旧不支持 CRI v1
        log_warn "检测到系统 containerd 包 (非 containerd.io)，将替换为 Docker 官方版本"
        apt-get remove -y -qq containerd 2>/dev/null || true
        NEED_INSTALL=true
    else
        log_info "containerd 已安装: $(containerd --version)"
    fi
    
    # 安装 Docker 官方 containerd.io
    if [[ "$NEED_INSTALL" == "true" ]]; then
        log_info "安装依赖包..."
        apt-get update -qq
        apt-get install -y -qq ca-certificates curl gnupg lsb-release apt-transport-https \
            socat conntrack ebtables ipset
        
        log_info "添加 Docker GPG key..."
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
        chmod a+r /etc/apt/keyrings/docker.gpg
        
        log_info "添加 Docker 仓库..."
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
        
        log_info "安装 containerd.io..."
        apt-get update -qq
        apt-get install -y -qq containerd.io
        
        NEED_CONFIG=true
    fi
    
    # 检查配置文件是否存在或需要重新生成
    if [[ ! -f /etc/containerd/config.toml ]]; then
        log_info "containerd 配置文件不存在，需要生成..."
        NEED_CONFIG=true
    fi
    
    # 配置 containerd
    if [[ "$NEED_CONFIG" == "true" ]]; then
        _configure_containerd
    else
        # 已有配置，仍需确保镜像加速已配置
        _ensure_mirror_config
    fi
    
    # 启动服务
    log_info "启动 containerd 服务..."
    systemctl daemon-reload
    systemctl enable containerd
    systemctl restart containerd
    
    # 配置 crictl 默认 endpoint (消除 WARN)
    if [[ ! -f /etc/crictl.yaml ]]; then
        cat > /etc/crictl.yaml <<EOF
runtime-endpoint: unix:///run/containerd/containerd.sock
image-endpoint: unix:///run/containerd/containerd.sock
timeout: 10
EOF
    fi
    
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
    
    if [[ ! -S /run/containerd/containerd.sock ]]; then
        log_error "containerd socket 不存在，请检查服务状态: systemctl status containerd"
        exit 1
    fi
    
    # ── 关键: 验证 CRI 插件已启用 ──
    log_info "验证 containerd CRI 插件..."
    sleep 2  # 给 CRI 插件初始化时间
    # 使用 ctr (containerd 自带) 检查 CRI 插件是否加载
    if ! ctr --address /run/containerd/containerd.sock version &>/dev/null; then
        log_error "containerd 服务异常，请检查: systemctl status containerd"
        exit 1
    fi
    # 检查 config.toml 中是否有 CRI 被禁用的痕迹
    if grep -q 'disabled_plugins.*cri' /etc/containerd/config.toml 2>/dev/null; then
        log_warn "config.toml 中 CRI 插件被禁用，强制重新配置..."
        _configure_containerd
        systemctl restart containerd
        sleep 3
    fi
    log_info "containerd CRI 插件验证通过"
    
    log_info "containerd 安装完成: $(containerd --version)"
}

#===============================================================================
# 确保镜像加速已配置 (重跑时补写 hosts.toml)
#===============================================================================
_ensure_mirror_config() {
    if [[ "$USE_CN_MIRROR" != "true" ]]; then
        return 0
    fi
    
    # 确保 sandbox_image 指向阿里云 (每次都检查)
    if grep -q 'registry.k8s.io/pause' /etc/containerd/config.toml 2>/dev/null; then
        sed -i 's|registry.k8s.io/pause|registry.aliyuncs.com/google_containers/pause|g' /etc/containerd/config.toml
        log_info "sandbox_image 已修正为阿里云镜像"
        local NEED_RESTART=true
    fi
    
    local CTD_MAJOR
    CTD_MAJOR=$(containerd --version 2>/dev/null | grep -oP '\d+\.\d+' | head -1 | cut -d. -f1 || echo "1")
    
    if [[ "$CTD_MAJOR" -ge 2 ]]; then
        # 检查 docker.io hosts.toml 是否已配置
        if [[ ! -f /etc/containerd/certs.d/docker.io/hosts.toml ]] || \
           ! grep -q 'swr.cn-north-4' /etc/containerd/certs.d/docker.io/hosts.toml 2>/dev/null; then
            log_info "补充配置 Docker Hub 镜像加速..."
            
            # 确保 config_path 已启用
            _set_containerd_config_path
            
            mkdir -p /etc/containerd/certs.d/docker.io
            cat > /etc/containerd/certs.d/docker.io/hosts.toml << 'TOML'
server = "https://docker.io"

[host."https://docker.m.daocloud.io"]
  capabilities = ["pull", "resolve"]
TOML
            log_info "Docker Hub hosts.toml 配置完成"
        fi
        
        if [[ ! -f /etc/containerd/certs.d/registry.k8s.io/hosts.toml ]]; then
            log_info "补充配置 registry.k8s.io 镜像加速..."
            mkdir -p /etc/containerd/certs.d/registry.k8s.io
            cat > /etc/containerd/certs.d/registry.k8s.io/hosts.toml << 'TOML'
server = "https://registry.k8s.io"

[host."https://registry.aliyuncs.com/google_containers"]
  capabilities = ["pull", "resolve"]
TOML
            log_info "registry.k8s.io hosts.toml 配置完成"
        fi
        
        # 重启让配置生效
        systemctl restart containerd 2>/dev/null || true
        sleep 2
    fi
}

#===============================================================================
# 安全设置 containerd config_path (避免重复插入)
#===============================================================================
_set_containerd_config_path() {
    local CONF="/etc/containerd/config.toml"
    if grep -q 'config_path.*"/etc/containerd/certs.d"' "$CONF" 2>/dev/null; then
        # 已正确配置
        return 0
    elif grep -q 'config_path\s*=' "$CONF" 2>/dev/null; then
        # config_path 存在但值不对 (如空字符串)，替换它
        sed -i 's|config_path\s*=.*|config_path = "/etc/containerd/certs.d"|' "$CONF"
        log_info "config_path 已更新为 /etc/containerd/certs.d"
    else
        # 不存在，插入到 registry section 后
        sed -i '/\[plugins.*registry\]/a\      config_path = "/etc/containerd/certs.d"' "$CONF" 2>/dev/null || true
        log_info "config_path 已添加"
    fi
}

_configure_containerd() {
    log_info "生成 containerd 配置..."
    mkdir -p /etc/containerd
    containerd config default > /etc/containerd/config.toml
    
    # 检测 containerd 主版本
    local CTD_MAJOR
    CTD_MAJOR=$(containerd --version 2>/dev/null | grep -oP '\d+\.\d+' | head -1 | cut -d. -f1 || echo "1")
    log_info "containerd 主版本: $CTD_MAJOR"
    
    # 启用 SystemdCgroup (containerd 1.x 和 2.x 路径不同)
    if [[ "$CTD_MAJOR" -ge 2 ]]; then
        # containerd 2.x: [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runc.options]
        sed -i 's/SystemdCgroup = false/SystemdCgroup = true/g' /etc/containerd/config.toml
    else
        # containerd 1.x: [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
        sed -i 's/SystemdCgroup = false/SystemdCgroup = true/g' /etc/containerd/config.toml
    fi
    
    # 确保 CRI 插件未被禁用 (兼容所有格式)
    # containerd 2.x 可能有 disabled_plugins = ["io.containerd.internal.v1.opt"]
    # 确保不包含任何 cri 相关插件
    local DISABLED_LINE
    DISABLED_LINE=$(grep '^disabled_plugins' /etc/containerd/config.toml 2>/dev/null || true)
    if [[ -n "$DISABLED_LINE" ]]; then
        if echo "$DISABLED_LINE" | grep -qi 'cri'; then
            log_warn "检测到 CRI 插件被禁用，正在移除..."
            sed -i 's/^disabled_plugins.*/disabled_plugins = []/' /etc/containerd/config.toml
        fi
    fi
    
    # 配置国内镜像加速
    if [[ "$USE_CN_MIRROR" == "true" ]]; then
        log_info "配置镜像加速..."
        
        # 将 sandbox_image 指向阿里云 (避免从 registry.k8s.io 拉取 pause 镜像)
        sed -i 's|registry.k8s.io/pause|registry.aliyuncs.com/google_containers/pause|g' /etc/containerd/config.toml
        log_info "sandbox_image 已指向阿里云镜像"
        
        if [[ "$CTD_MAJOR" -ge 2 ]]; then
            # ── containerd 2.x: hosts.toml 格式 ──
            log_info "containerd 2.x，使用 hosts.toml 镜像配置..."
            
            # 确保 config_path 已在 config.toml 中启用
            _set_containerd_config_path
            
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
            sed -i 's|registry.k8s.io|registry.aliyuncs.com/google_containers|g' /etc/containerd/config.toml
            
            if ! grep -q 'registry.mirrors.*docker.io' /etc/containerd/config.toml; then
                log_info "配置 Docker Hub 镜像加速..."
                cat >> /etc/containerd/config.toml << 'EOF'

# Docker Hub 镜像加速 - DaoCloud
[plugins."io.containerd.grpc.v1.cri".registry.mirrors."docker.io"]
  endpoint = ["https://docker.m.daocloud.io"]
EOF
            fi
        fi
    fi
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
    
    curl -fsSL "https://pkgs.k8s.io/core:/stable:/v${K8S_MINOR_VERSION}/deb/Release.key" | gpg --dearmor --yes -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg 2>/dev/null
    echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v${K8S_MINOR_VERSION}/deb/ /" > /etc/apt/sources.list.d/kubernetes.list
    
    # 安装 kubeadm, kubelet, kubectl
    if command -v kubeadm &>/dev/null && command -v kubelet &>/dev/null && command -v kubectl &>/dev/null; then
        local INSTALLED_K8S
        INSTALLED_K8S=$(kubeadm version -o short 2>/dev/null | sed 's/v//' || true)
        if [[ "$INSTALLED_K8S" == "$K8S_VERSION" ]]; then
            log_info "kubeadm/kubelet/kubectl v$K8S_VERSION 已安装，跳过"
        else
            log_info "当前版本 v$INSTALLED_K8S → 目标 v$K8S_VERSION，升级中..."
            apt-get update -qq
            apt-get install -y -qq kubelet kubeadm kubectl
            apt-mark hold kubelet kubeadm kubectl
        fi
    else
        log_info "安装 kubeadm, kubelet, kubectl..."
        apt-get update -qq
        apt-get install -y -qq kubelet kubeadm kubectl
        apt-mark hold kubelet kubeadm kubectl
    fi
    
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
        log_warn "K8s 集群已初始化，跳过 kubeadm init..."
        # 确保 kubectl 配置可用
        mkdir -p $HOME/.kube
        cp -f /etc/kubernetes/admin.conf $HOME/.kube/config 2>/dev/null || true
        chown $(id -u):$(id -g) $HOME/.kube/config
        export KUBECONFIG=$KUBE_CONFIG
        # 仍然需要确保单节点可调度 (重跑时污点可能已恢复)
        _post_init_configure
        return 0
    fi
    
    # 预拉取镜像
    log_info "预拉取 K8s 镜像..."
    if [[ "$USE_CN_MIRROR" == "true" ]]; then
        kubeadm config images pull --image-repository registry.aliyuncs.com/google_containers 2>&1 | tee /tmp/kubeadm-pull.log
    else
        kubeadm config images pull 2>&1 | tee /tmp/kubeadm-pull.log
    fi
    
    # 验证核心镜像已就绪
    local MISSING_IMAGES=0
    for comp in kube-apiserver kube-controller-manager kube-scheduler; do
        if ! crictl images 2>/dev/null | grep -q "$comp"; then
            log_warn "镜像未就绪: $comp"
            MISSING_IMAGES=1
        fi
    done
    if [[ "$MISSING_IMAGES" == "1" ]]; then
        log_warn "部分镜像未拉取成功，尝试重新拉取..."
        if [[ "$USE_CN_MIRROR" == "true" ]]; then
            kubeadm config images pull --image-repository registry.aliyuncs.com/google_containers 2>&1 || true
        fi
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
    
    kubeadm init $INIT_OPTS --upload-certs --v=1 2>&1 | tee /tmp/kubeadm-init.log
    
    # 配置 kubectl
    log_info "配置 kubectl..."
    mkdir -p $HOME/.kube
    cp -f /etc/kubernetes/admin.conf $HOME/.kube/config
    chown $(id -u):$(id -g) $HOME/.kube/config
    export KUBECONFIG=$KUBE_CONFIG
    
    # 初始化后配置
    _post_init_configure
    
    log_info "K8s 集群初始化完成"
}

_post_init_configure() {
    # 允许 master 节点调度 Pod (单节点集群必须)
    log_info "配置 master 节点可调度..."
    kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>/dev/null || true
    
    # 配置 kube-proxy 禁止在边缘节点运行
    log_info "配置 kube-proxy 排除边缘节点..."
    kubectl -n kube-system patch daemonset kube-proxy --type merge -p '
    {"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"node-role.kubernetes.io/edge","operator":"DoesNotExist"}]}]}}}}}}}' 2>/dev/null || true
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
        # 下载成功后缓存到 cache 目录
        mkdir -p "$SCRIPT_DIR/cache"
        cp "/tmp/$CALICO_FILE" "$SCRIPT_DIR/cache/" 2>/dev/null || true
    fi
    
    # 修改 CIDR 配置 (默认 192.168.0.0/16 改为 $POD_CIDR)
    if [[ "$POD_CIDR" != "192.168.0.0/16" ]]; then
        log_info "配置 Pod CIDR: $POD_CIDR"
        sed -i "s|192.168.0.0/16|$POD_CIDR|g" calico.yaml
        # 取消注释 CALICO_IPV4POOL_CIDR
        sed -i 's/# - name: CALICO_IPV4POOL_CIDR/- name: CALICO_IPV4POOL_CIDR/' calico.yaml
        sed -i "s|#   value: \"192.168.0.0/16\"|  value: \"$POD_CIDR\"|" calico.yaml
    fi
    
    # 预拉取 Calico 镜像 (国内环境 docker.io 不稳定，从华为云镜像拉取后重命名)
    if [[ "$USE_CN_MIRROR" == "true" ]]; then
        log_info "从国内镜像预拉取 Calico 镜像..."
        local MIRROR="swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io"
        local CALICO_IMAGES=("calico/cni" "calico/node" "calico/kube-controllers")
        for img in "${CALICO_IMAGES[@]}"; do
            local SRC="${MIRROR}/${img}:${CALICO_VERSION}"
            local DST="docker.io/${img}:${CALICO_VERSION}"
            if ctr -n k8s.io images check "name==${DST}" 2>/dev/null | grep -q "${DST}"; then
                log_info "镜像已存在: ${DST}"
            else
                log_info "拉取: ${SRC}"
                ctr -n k8s.io images pull "${SRC}" && \
                ctr -n k8s.io images tag "${SRC}" "${DST}" && \
                log_info "完成: ${DST}" || \
                log_warn "镜像拉取失败: ${img}，将回退到直接拉取"
            fi
        done
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
        # 下载成功后缓存到脚本目录的 cache 子目录
        mkdir -p "$SCRIPT_DIR/cache"
        cp "/tmp/$KEADM_FILE" "$SCRIPT_DIR/cache/" 2>/dev/null || true
    fi
    
    tar -xzf "$KEADM_FILE"
    cp keadm-v${KUBEEDGE_VERSION}-linux-${ARCH}/keadm/keadm /usr/local/bin/
    chmod +x /usr/local/bin/keadm
    rm -rf keadm-v${KUBEEDGE_VERSION}-linux-${ARCH}/
    rm -f "$KEADM_FILE"  # 删除/tmp中的文件，cache目录已保留
    
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
    
    # 初始化 CloudCore (内网IP+公网IP 双地址证书)
    local INTERNAL_IP
    INTERNAL_IP=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[\d.]+' || true)
    local ADVERTISE_ADDR
    if [[ -n "$INTERNAL_IP" && "$INTERNAL_IP" != "$CLOUD_PUBLIC_IP" ]]; then
        ADVERTISE_ADDR="${INTERNAL_IP},${CLOUD_PUBLIC_IP}"
        log_info "  advertise-address: $ADVERTISE_ADDR (内网+公网)"
    else
        ADVERTISE_ADDR="$CLOUD_PUBLIC_IP"
        log_info "  advertise-address: $ADVERTISE_ADDR"
    fi
    
    # 预拉取 KubeEdge 镜像 (加速启动)
    log_info "预拉取 KubeEdge 镜像..."
    for img in "kubeedge/cloudcore:v${KUBEEDGE_VERSION}" "kubeedge/iptables-manager:v${KUBEEDGE_VERSION}"; do
        if ! crictl images 2>/dev/null | grep -q "$(echo $img | cut -d: -f1)"; then
            log_info "拉取 $img ..."
            ctr -n k8s.io images pull "docker.io/$img" 2>&1 | tail -1 || true
        else
            log_info "$img 已存在"
        fi
    done
    
    keadm init --advertise-address="$ADVERTISE_ADDR" \
               --kubeedge-version="$KUBEEDGE_VERSION" \
               --kube-config=$KUBE_CONFIG \
               --set cloudCore.modules.dynamicController.enable=true \
               --set cloudCore.modules.cloudStream.enable=true \
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
    
    # 容器化部署: 通过 ConfigMap 修改配置
    local CM_DATA
    CM_DATA=$(kubectl get configmap cloudcore -n kubeedge -o jsonpath='{.data.cloudcore\.yaml}' 2>/dev/null || true)
    
    if [[ -n "$CM_DATA" ]]; then
        log_info "检测到容器化部署 (ConfigMap)"
        
        # 检查 cloudStream 是否已启用
        if echo "$CM_DATA" | grep -A2 'cloudStream:' | grep -q 'enable: true'; then
            log_info "cloudStream 已启用"
        else
            log_info "启用 cloudStream..."
            # 获取 ConfigMap 并修改
            local TMPFILE=$(mktemp)
            kubectl get configmap cloudcore -n kubeedge -o yaml > "$TMPFILE"
            # 在 cloudStream section 下将 enable: false 改为 enable: true
            sed -i '/cloudStream:/,/enable:/{s/enable: false/enable: true/}' "$TMPFILE"
            kubectl apply -f "$TMPFILE" 2>/dev/null
            rm -f "$TMPFILE"
            log_info "ConfigMap 已更新"
        fi
    elif [[ -f "/etc/kubeedge/config/cloudcore.yaml" ]]; then
        # 二进制部署: 直接修改配置文件
        log_info "检测到二进制部署"
        local CLOUDCORE_CONFIG="/etc/kubeedge/config/cloudcore.yaml"
        cp "$CLOUDCORE_CONFIG" "${CLOUDCORE_CONFIG}.bak"
        sed -i '/cloudStream:/,/enable:/{s/enable: false/enable: true/}' "$CLOUDCORE_CONFIG"
        log_info "cloudcore.yaml 已更新"
    else
        log_warn "CloudCore 配置未找到 (ConfigMap 和本地文件均不存在)"
        return 0
    fi
    
    # 重启 CloudCore Pod 应用配置
    log_info "重启 CloudCore..."
    # 通过 Pod 名称匹配而非 label (不同版本 label 可能不同)
    local CC_POD
    CC_POD=$(kubectl get pods -n kubeedge --no-headers 2>/dev/null | grep cloudcore | awk '{print $1}' | head -1)
    if [[ -n "$CC_POD" ]]; then
        kubectl delete pod -n kubeedge "$CC_POD" 2>/dev/null || true
    fi
    sleep 5
    
    # 等待 CloudCore 就绪
    local CLOUDCORE_POD_IP=""
    for i in {1..30}; do
        if kubectl get pods -n kubeedge --no-headers 2>/dev/null | grep -q "cloudcore.*Running"; then
            log_info "CloudCore 重启成功"
            sleep 3
            # 通过 Pod 名称获取 IP
            CC_POD=$(kubectl get pods -n kubeedge --no-headers 2>/dev/null | grep cloudcore | awk '{print $1}' | head -1)
            CLOUDCORE_POD_IP=$(kubectl get pod -n kubeedge "$CC_POD" -o jsonpath='{.status.podIP}' 2>/dev/null || true)
            break
        fi
        log_info "等待 CloudCore 重启... ($i/30)"
        sleep 3
    done
    
    # 配置 iptables 转发规则: 10350 → CloudCore 10003 (重启后配置，确保 Pod IP 可用)
    if [[ -n "$CLOUDCORE_POD_IP" ]]; then
        log_info "配置 iptables 转发: 10350 → $CLOUDCORE_POD_IP:10003"
        iptables -t nat -D OUTPUT -p tcp --dport 10350 -j DNAT --to $CLOUDCORE_POD_IP:10003 2>/dev/null || true
        iptables -t nat -A OUTPUT -p tcp --dport 10350 -j DNAT --to $CLOUDCORE_POD_IP:10003
        log_info "iptables 转发规则配置完成"
    else
        log_warn "无法获取 CloudCore Pod IP，请手动配置 iptables"
    fi
    
    log_info "CloudStream 配置完成"
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
    # 等待 CloudCore 创建 tokensecret (最多等 60s)
    local TOKEN=""
    for i in {1..12}; do
        TOKEN=$(keadm gettoken --kube-config=$KUBE_CONFIG 2>/dev/null) && break
        log_info "等待 CloudCore 初始化 token... ($i/12)"
        sleep 5
    done
    
    if [[ -z "$TOKEN" ]]; then
        log_error "Token 获取失败，请检查 CloudCore 是否正常运行:"
        log_error "  kubectl get pods -n kubeedge"
        log_error "  kubectl get secret -n kubeedge tokensecret"
        log_error "稍后手动执行: keadm gettoken --kube-config=$KUBE_CONFIG"
        return 1
    fi
    
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
    get_k8s_version
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
    
    get_kubeedge_version
    
    if [[ -z "$CLOUD_PUBLIC_IP" ]]; then
        log_error "请使用 --cloud-ip 参数指定云主机公网 IP"
        exit 1
    fi
    
    if [[ ! -f /etc/kubernetes/admin.conf ]]; then
        log_error "K8s 集群未初始化，请先执行 install-k8s"
        exit 1
    fi
    
    check_public_ip
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
