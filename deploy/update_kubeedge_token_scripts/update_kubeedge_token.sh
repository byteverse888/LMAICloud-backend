#!/bin/bash

# 配置
API_URL="${API_URL:-http://115.190.25.82:8883/parseapi/config}"
APP_ID="${APP_ID:-BTGAPPId}"
MASTER_KEY="${MASTER_KEY:-BTGMASTERKEY123}"
CLUSTERS="${CLUSTERS:-beijing:115.190.25.82}"

# 检查命令
command -v keadm >/dev/null 2>&1 || { echo "Error: keadm not found"; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "Error: curl not found"; exit 1; }

# 获取token
get_token() {
    local token=$(sudo keadm gettoken 2>/dev/null)
    if [ -z "$token" ] || [[ "$token" == *"Error"* ]]; then
        echo "Error: Failed to get token for $1" >&2
        return 1
    fi
    echo "$token"
}

# 构建JSON
json="{\"params\":{\"clouds\":["
first=1

for cluster in $CLUSTERS; do
    IFS=':' read -r name ip <<< "$cluster"
    token=$(get_token "$name") || exit 1
    
    [ $first -eq 1 ] && first=0 || json+=","
    # 转义引号
    token=$(echo "$token" | sed 's/"/\\"/g')
    json+="{\"cloudname\":\"$name\",\"ip\":\"$ip\",\"token\":\"$token\"}"
done

json+="]}}"

# 更新API
curl -s -o /dev/null -w "%{http_code}" \
    -X PUT "$API_URL" \
    -H "X-Parse-Application-Id: $APP_ID" \
    -H "X-Parse-Master-Key: $MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "$json" | grep -q "^2" && echo "Success" || echo "Failed"
