-- Migration: instances 表支持 K8s 直查节点（node_id 可为空，新增 node_name）
-- 执行前确认当前数据库状态

-- 1. node_id 改为可空（边缘节点可能不在 DB nodes 表中）
ALTER TABLE instances ALTER COLUMN node_id DROP NOT NULL;

-- 2. 新增 node_name 列存储 K8s 节点名称
ALTER TABLE instances ADD COLUMN IF NOT EXISTS node_name VARCHAR(100);

-- 3. 回填 node_name：从 nodes 表获取已有实例的节点名
UPDATE instances SET node_name = nodes.name
FROM nodes WHERE instances.node_id = nodes.id AND instances.node_name IS NULL;
