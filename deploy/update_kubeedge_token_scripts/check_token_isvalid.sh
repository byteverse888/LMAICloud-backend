sudo keadm gettoken | python3 -c "
import sys, base64, json, datetime

token = sys.stdin.read().strip()
parts = token.split('.')

# KubeEdge 格式: [前缀].[header].[payload].[signature]
# payload 是第3段（索引2）
payload = parts[2]

# 添加 padding
padding = 4 - len(payload) % 4
if padding < 4:
    payload += '=' * padding

decoded = base64.b64decode(payload)
data = json.loads(decoded)

print('=== Token 时间信息 ===')
print(f'完整 Payload: {json.dumps(data, indent=2)}')
print()
if 'exp' in data:
    print(f'过期时间 (exp): {datetime.datetime.fromtimestamp(data[\"exp\"])}')
    print(f'当前时间: {datetime.datetime.now()}')
    remaining = data['exp'] - datetime.datetime.now().timestamp()
    if remaining > 0:
        print(f'剩余有效期: {remaining/3600:.1f} 小时 ({remaining:.0f} 秒)')
    else:
        print(f'⚠️ Token 已过期 {abs(remaining)/3600:.1f} 小时！')
"
