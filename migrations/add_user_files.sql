-- 用户文件管理系统迁移脚本
-- 1. AIUser 新增存储配额字段
ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS storage_quota BIGINT DEFAULT 10737418240;  -- 10GB
ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS storage_used  BIGINT DEFAULT 0;

-- 2. 新建 user_files 表
CREATE TABLE IF NOT EXISTS user_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES ai_users(id),
    parent_id UUID REFERENCES user_files(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    path VARCHAR(1000) NOT NULL,
    is_dir BOOLEAN DEFAULT FALSE,
    size BIGINT DEFAULT 0,
    mime_type VARCHAR(100),
    storage_backend VARCHAR(20) DEFAULT 'ipfs',
    storage_key VARCHAR(500),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    -- 有父目录时的唯一约束
    CONSTRAINT uq_user_parent_name UNIQUE(user_id, parent_id, name)
);

-- 根目录下唯一约束 (PG 中 NULL!=NULL, UNIQUE 约束对 NULL 列不生效, 需要 partial index)
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_root_name
    ON user_files(user_id, name) WHERE parent_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_user_files_user_parent ON user_files(user_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_user_files_path ON user_files(user_id, path);
