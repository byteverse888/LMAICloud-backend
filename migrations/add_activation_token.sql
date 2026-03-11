-- 邮箱激活功能数据库迁移脚本
-- 执行此脚本添加激活令牌相关字段

-- 添加激活相关字段到 ai_users 表
ALTER TABLE ai_users 
ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT TRUE,
ADD COLUMN IF NOT EXISTS activation_token VARCHAR(100),
ADD COLUMN IF NOT EXISTS activation_expires_at TIMESTAMP;

-- 将现有用户设为已验证
UPDATE ai_users SET verified = TRUE WHERE verified IS NULL;

-- 为激活令牌创建索引以加快查询
CREATE INDEX IF NOT EXISTS idx_ai_users_activation_token 
ON ai_users(activation_token);

-- 验证字段是否添加成功
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'ai_users' 
AND column_name IN ('activation_token', 'activation_expires_at', 'verified');
