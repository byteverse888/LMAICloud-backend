"""
数据库统一迁移脚本 - 合并所有迁移
按依赖顺序执行，每步幂等（可重复运行）

包含迁移:
  1.   邮箱激活字段 (add_activation_token)
  2.   实例 node_name (add_node_name_to_instances)
  2.5  Instance 表 V2 字段升级 (gpu_model/image_url/自动关机等20列)
  3.   实例 namespace (add_namespace_to_instances)
  4.   用户文件系统 (add_user_files)
  5.   OpenClaw 模块 (add_openclaw_tables)
  6.   计费系统升级 (add_billing_upgrade) -- 依赖 openclaw_instances
  7.   平台功能升级 (add_platform_upgrade + platform_upgrade_v2)
  8.   预置种子数据 (resource_plans / market_products / app_images / images)
  9.   按实际运行时长计费改造 (billing_records + last_billed_at)
  10.  BillingType 枚举补全 (daily/weekly/yearly)
  11.  OpenClaw 实例计费字段
  12.  billing_records 计费对象字段
  13.  ai_users 实例配额字段
  14.  审计日志支持登录失败记录
  15.  OpenClaw 监控字段补全 (check_message)

用法:
  cd LMAICloud-backend
  python run_migration_all.py
"""
import asyncio
import sys
import asyncpg
from sqlalchemy import text
from app.database import engine
from app.config import settings


# ============================================================
# 迁移步骤定义 —— (名称, SQL列表)
# 每条 SQL 独立执行，遇到 "已存在" 类错误自动跳过
# ============================================================

MIGRATIONS: list[tuple[str, list[str]]] = [

    # ── 1. 邮箱激活字段 ──────────────────────────────────────
    ("1. 邮箱激活字段", [
        """ALTER TABLE ai_users
           ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT TRUE""",

        """ALTER TABLE ai_users
           ADD COLUMN IF NOT EXISTS activation_token VARCHAR(100)""",

        """ALTER TABLE ai_users
           ADD COLUMN IF NOT EXISTS activation_expires_at TIMESTAMP""",

        "UPDATE ai_users SET verified = TRUE WHERE verified IS NULL",

        """CREATE INDEX IF NOT EXISTS idx_ai_users_activation_token
           ON ai_users(activation_token)""",
    ]),

    # ── 2. 实例 node_name ────────────────────────────────────
    ("2. 实例 node_name / node_id 可空", [
        "ALTER TABLE instances ALTER COLUMN node_id DROP NOT NULL",

        """ALTER TABLE instances
           ADD COLUMN IF NOT EXISTS node_name VARCHAR(100)""",

        """UPDATE instances SET node_name = nodes.name
           FROM nodes
           WHERE instances.node_id = nodes.id
             AND instances.node_name IS NULL""",
    ]),

    # ── 2.5 Instance 表 V2 字段升级 ────────────────────────
    ("2.5 Instance 表 V2 字段升级 (20列)", [
        # 资源与节点
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS gpu_model VARCHAR(100)",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS resource_type VARCHAR(20) DEFAULT 'vGPU'",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS node_type VARCHAR(20) DEFAULT 'center'",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS instance_count INTEGER DEFAULT 1",
        # 镜像与启动
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS image_url VARCHAR(500)",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS startup_command VARCHAR(2000)",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS env_vars TEXT",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS storage_mounts TEXT",
        # 安装源
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS pip_source VARCHAR(100)",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS conda_source VARCHAR(100)",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS apt_source VARCHAR(100)",
        # 自动关机/释放
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_shutdown_type VARCHAR(20) DEFAULT 'none'",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_shutdown_minutes INTEGER",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_shutdown_time TIMESTAMP",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_release_type VARCHAR(20) DEFAULT 'none'",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_release_minutes INTEGER",
        # 连接与状态
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS internal_ip VARCHAR(50)",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS deployment_yaml TEXT",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS health_status VARCHAR(20) DEFAULT 'unknown'",
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS release_at TIMESTAMP",
        # 计费与到期
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS expired_at TIMESTAMP",
    ]),

    # ── 3. 实例 namespace ────────────────────────────────────
    ("3. 实例 namespace (K8s)", [
        """ALTER TABLE instances
           ADD COLUMN IF NOT EXISTS namespace VARCHAR(63) DEFAULT 'lmaicloud'""",

        """UPDATE instances
           SET namespace = 'lmai-' || LEFT(REPLACE(CAST(user_id AS TEXT), '-', ''), 8)
           WHERE namespace = 'lmaicloud' OR namespace IS NULL""",
    ]),

    # ── 4. 用户文件系统 ──────────────────────────────────────
    ("4. 用户文件系统", [
        """ALTER TABLE ai_users
           ADD COLUMN IF NOT EXISTS storage_quota BIGINT DEFAULT 10737418240""",

        """ALTER TABLE ai_users
           ADD COLUMN IF NOT EXISTS storage_used BIGINT DEFAULT 0""",

        """CREATE TABLE IF NOT EXISTS user_files (
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
               CONSTRAINT uq_user_parent_name UNIQUE(user_id, parent_id, name)
           )""",

        """CREATE UNIQUE INDEX IF NOT EXISTS uq_user_root_name
           ON user_files(user_id, name) WHERE parent_id IS NULL""",

        """CREATE INDEX IF NOT EXISTS idx_user_files_user_parent
           ON user_files(user_id, parent_id)""",

        """CREATE INDEX IF NOT EXISTS idx_user_files_path
           ON user_files(user_id, path)""",
    ]),

    # ── 5. OpenClaw 模块 ─────────────────────────────────────
    ("5. OpenClaw 模块 (4张表)", [
        """CREATE TABLE IF NOT EXISTS openclaw_instances (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               user_id UUID NOT NULL REFERENCES ai_users(id),
               name VARCHAR(100) NOT NULL,
               status VARCHAR(20) DEFAULT 'creating',
               namespace VARCHAR(63),
               node_name VARCHAR(200),
               node_type VARCHAR(20) DEFAULT 'center',
               cpu_cores INTEGER DEFAULT 2,
               memory_gb INTEGER DEFAULT 4,
               disk_gb INTEGER DEFAULT 20,
               image_url VARCHAR(500),
               port INTEGER DEFAULT 18789,
               deployment_name VARCHAR(200),
               service_name VARCHAR(200),
               internal_ip VARCHAR(50),
               gateway_token VARCHAR(200),
               started_at TIMESTAMP,
               created_at TIMESTAMP DEFAULT NOW(),
               updated_at TIMESTAMP DEFAULT NOW()
           )""",

        """CREATE INDEX IF NOT EXISTS idx_openclaw_instances_user
           ON openclaw_instances(user_id)""",

        """CREATE INDEX IF NOT EXISTS idx_openclaw_instances_status
           ON openclaw_instances(status)""",

        """CREATE TABLE IF NOT EXISTS openclaw_model_keys (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               instance_id UUID NOT NULL REFERENCES openclaw_instances(id) ON DELETE CASCADE,
               provider VARCHAR(50) NOT NULL,
               alias VARCHAR(100),
               api_key VARCHAR(500),
               base_url VARCHAR(300),
               model_name VARCHAR(100),
               is_active BOOLEAN DEFAULT TRUE,
               last_check_at TIMESTAMP,
               check_status VARCHAR(20),
               balance DOUBLE PRECISION,
               tokens_used BIGINT DEFAULT 0,
               created_at TIMESTAMP DEFAULT NOW(),
               updated_at TIMESTAMP DEFAULT NOW()
           )""",

        """CREATE INDEX IF NOT EXISTS idx_openclaw_model_keys_instance
           ON openclaw_model_keys(instance_id)""",

        """CREATE TABLE IF NOT EXISTS openclaw_channels (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               instance_id UUID NOT NULL REFERENCES openclaw_instances(id) ON DELETE CASCADE,
               type VARCHAR(30) NOT NULL,
               name VARCHAR(100),
               config TEXT,
               is_active BOOLEAN DEFAULT TRUE,
               online_status VARCHAR(20),
               last_check_at TIMESTAMP,
               created_at TIMESTAMP DEFAULT NOW(),
               updated_at TIMESTAMP DEFAULT NOW()
           )""",

        """CREATE INDEX IF NOT EXISTS idx_openclaw_channels_instance
           ON openclaw_channels(instance_id)""",

        """CREATE TABLE IF NOT EXISTS openclaw_skills (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               instance_id UUID NOT NULL REFERENCES openclaw_instances(id) ON DELETE CASCADE,
               name VARCHAR(100) NOT NULL,
               description TEXT,
               status VARCHAR(20) DEFAULT 'installing',
               version VARCHAR(20),
               installed_at TIMESTAMP,
               created_at TIMESTAMP DEFAULT NOW(),
               updated_at TIMESTAMP DEFAULT NOW()
           )""",

        """CREATE INDEX IF NOT EXISTS idx_openclaw_skills_instance
           ON openclaw_skills(instance_id)""",
    ]),

    # ── 6. 计费系统升级 (依赖 openclaw_instances) ────────────
    ("6. 计费系统升级", [
        """CREATE TABLE IF NOT EXISTS resource_plans (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               name VARCHAR(100) NOT NULL,
               description TEXT,
               plan_type VARCHAR(20) NOT NULL DEFAULT 'PACKAGE',
               billing_cycle VARCHAR(20) NOT NULL DEFAULT 'MONTHLY',
               cpu_cores INTEGER NOT NULL DEFAULT 0,
               memory_gb INTEGER NOT NULL DEFAULT 0,
               gpu_count INTEGER NOT NULL DEFAULT 0,
               gpu_model VARCHAR(100),
               disk_gb INTEGER NOT NULL DEFAULT 0,
               price DOUBLE PRECISION NOT NULL,
               original_price DOUBLE PRECISION,
               is_active BOOLEAN NOT NULL DEFAULT TRUE,
               sort_order INTEGER NOT NULL DEFAULT 0,
               created_at TIMESTAMP DEFAULT now(),
               updated_at TIMESTAMP DEFAULT now()
           )""",

        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS product_name VARCHAR(100)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS billing_cycle VARCHAR(20)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS openclaw_instance_id UUID REFERENCES openclaw_instances(id)",

        "ALTER TABLE recharges ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP",
    ]),

    # ── 7. 平台功能升级 (积分/日志/通知/市场/系统设置) ───────
    ("7. 平台功能升级 — 枚举类型", [
        """DO $$ BEGIN
               CREATE TYPE pointtype AS ENUM ('recharge_reward','daily_login','invite_reward','consume');
           EXCEPTION WHEN duplicate_object THEN NULL; END $$""",

        """DO $$ BEGIN
               CREATE TYPE auditaction AS ENUM ('create','delete','update','start','stop','restart','login','recharge');
           EXCEPTION WHEN duplicate_object THEN NULL; END $$""",

        """DO $$ BEGIN
               CREATE TYPE auditresourcetype AS ENUM ('instance','openclaw','storage','image','account','billing');
           EXCEPTION WHEN duplicate_object THEN NULL; END $$""",

        """DO $$ BEGIN
               CREATE TYPE notificationtype AS ENUM ('system','billing','instance','points');
           EXCEPTION WHEN duplicate_object THEN NULL; END $$""",

        """DO $$ BEGIN
               CREATE TYPE marketcategory AS ENUM ('compute','ai_app');
           EXCEPTION WHEN duplicate_object THEN NULL; END $$""",
    ]),

    ("7. 平台功能升级 — AIUser 新字段", [
        "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0",
        "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS invite_code VARCHAR(20)",
        "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS invited_by UUID REFERENCES ai_users(id)",
        "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS last_checkin_date VARCHAR(10)",

        """CREATE UNIQUE INDEX IF NOT EXISTS ix_ai_users_invite_code
           ON ai_users(invite_code) WHERE invite_code IS NOT NULL""",
    ]),

    ("7. 平台功能升级 — 积分流水表", [
        """CREATE TABLE IF NOT EXISTS point_records (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               user_id UUID NOT NULL REFERENCES ai_users(id),
               points INTEGER NOT NULL,
               type pointtype NOT NULL,
               description VARCHAR(500),
               created_at TIMESTAMP DEFAULT NOW()
           )""",

        "CREATE INDEX IF NOT EXISTS ix_point_records_user_id ON point_records(user_id)",
    ]),

    ("7. 平台功能升级 — 操作日志表", [
        """CREATE TABLE IF NOT EXISTS audit_logs (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               user_id UUID NOT NULL REFERENCES ai_users(id),
               action auditaction NOT NULL,
               resource_type auditresourcetype NOT NULL,
               resource_id VARCHAR(100),
               resource_name VARCHAR(200),
               detail TEXT,
               ip_address VARCHAR(50),
               created_at TIMESTAMP DEFAULT NOW()
           )""",

        "CREATE INDEX IF NOT EXISTS ix_audit_logs_user_id ON audit_logs(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs(created_at DESC)",
    ]),

    ("7. 平台功能升级 — 通知表", [
        """CREATE TABLE IF NOT EXISTS notifications (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               user_id UUID NOT NULL REFERENCES ai_users(id),
               title VARCHAR(200) NOT NULL,
               content TEXT,
               type notificationtype DEFAULT 'system',
               is_read BOOLEAN DEFAULT FALSE,
               created_at TIMESTAMP DEFAULT NOW()
           )""",

        "CREATE INDEX IF NOT EXISTS ix_notifications_user_id ON notifications(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_notifications_unread ON notifications(user_id, is_read) WHERE is_read = FALSE",
    ]),

    ("7. 平台功能升级 — 市场产品表", [
        """CREATE TABLE IF NOT EXISTS market_products (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               category marketcategory NOT NULL,
               name VARCHAR(200) NOT NULL,
               description TEXT,
               icon VARCHAR(500),
               specs TEXT,
               price FLOAT DEFAULT 0,
               price_unit VARCHAR(50) DEFAULT '元/小时',
               tags TEXT,
               sort_order INTEGER DEFAULT 0,
               is_active BOOLEAN DEFAULT TRUE,
               created_at TIMESTAMP DEFAULT NOW(),
               updated_at TIMESTAMP DEFAULT NOW()
           )""",
    ]),

    ("7. 平台功能升级 — 系统设置表 & 默认值", [
        """CREATE TABLE IF NOT EXISTS system_settings (
               key VARCHAR(100) PRIMARY KEY,
               value TEXT,
               description VARCHAR(255),
               updated_at TIMESTAMP DEFAULT NOW()
           )""",

        """INSERT INTO system_settings (key, value, description) VALUES
               ('site_name',          '"貔貅云"',                              '站点名称'),
               ('site_description',   '"大模型AI算力云平台"',                       '站点描述'),
               ('site_logo',          '""',                                       '站点Logo URL'),
               ('contact_email',      '"support@lmaicloud.com"',                  '客服邮箱'),
               ('footer_text',        '""',                                       '页脚自定义文字'),
               ('icp_number',         '""',                                       'ICP备案号'),
               ('icp_link',           '"https://beian.miit.gov.cn/"',             'ICP备案链接'),
               ('police_number',      '""',                                       '公安备案号'),
               ('copyright_text',     '"© 2026 巴特星球. All rights reserved."', '版权信息'),
               ('captcha_enabled',    'true',                                     '是否启用登录验证码'),
               ('user_agreement',     '""',                                       '用户协议'),
               ('privacy_policy',     '""',                                       '隐私政策'),
               ('service_agreement',  '""',                                       '产品服务协议'),
               ('default_balance',           '0.0',    '新用户默认余额'),
               ('min_recharge_amount',       '10.0',   '最低充值金额'),
               ('max_recharge_amount',       '100000.0','最高充值金额'),
               ('instance_auto_stop_hours',  '24',     '实例自动停止(小时)'),
               ('instance_max_per_user',     '10',     '每用户最大实例数'),
               ('storage_max_gb_per_user',   '100',    '每用户存储上限(GB)'),
               ('price_adjustment_rate',     '1.0',    '价格调整系数'),
               ('maintenance_mode',          'false',  '维护模式'),
               ('registration_enabled',      'true',   '允许注册'),
               ('email_verification_required','true',  '邮箱验证必填'),
               ('notification_email_enabled','true',   '通知邮件启用')
           ON CONFLICT (key) DO NOTHING""",
    ]),

    # ── 8. 预置种子数据 ──────────────────────────────────────
    ("8. 预置资源套餐 (resource_plans)", [
        """INSERT INTO resource_plans (id, name, description, plan_type, billing_cycle,
               cpu_cores, memory_gb, gpu_count, gpu_model, disk_gb, price, original_price, is_active, sort_order,
               created_at, updated_at)
           VALUES
               (gen_random_uuid(), '入门GPU', 'RTX 3090 入门算力，适合模型微调和小规模推理',
                'PACKAGE', 'HOURLY', 8, 16, 1, 'RTX 3090', 50, 2.0, 2.5, true, 10, NOW(), NOW()),

               (gen_random_uuid(), '标准GPU', 'RTX 4090 标准算力，适合中等规模训练和推理',
                'PACKAGE', 'HOURLY', 16, 32, 1, 'RTX 4090', 100, 3.5, 4.5, true, 20, NOW(), NOW()),

               (gen_random_uuid(), '专业GPU', 'A100 40G 专业算力，适合大模型训练',
                'PACKAGE', 'HOURLY', 32, 64, 1, 'A100 40G', 200, 12.0, 15.0, true, 30, NOW(), NOW()),

               (gen_random_uuid(), '旗舰GPU', 'A100 80G 旗舰算力，适合大规模分布式训练',
                'PACKAGE', 'HOURLY', 64, 128, 1, 'A100 80G', 500, 18.0, 22.0, true, 40, NOW(), NOW()),

               (gen_random_uuid(), '顶配GPU', 'H100 80G 顶级算力，适合超大模型和高性能计算',
                'PACKAGE', 'HOURLY', 64, 256, 1, 'H100 80G', 500, 25.0, 30.0, true, 50, NOW(), NOW()),

               (gen_random_uuid(), '入门月卡', 'RTX 3090 包月套餐，性价比之选',
                'PACKAGE', 'MONTHLY', 8, 16, 1, 'RTX 3090', 50, 999.0, 1440.0, true, 60, NOW(), NOW()),

               (gen_random_uuid(), '标准月卡', 'RTX 4090 包月套餐，稳定高效',
                'PACKAGE', 'MONTHLY', 16, 32, 1, 'RTX 4090', 100, 1799.0, 2520.0, true, 70, NOW(), NOW()),

               (gen_random_uuid(), '专业月卡', 'A100 40G 包月套餐，专业训练首选',
                'PACKAGE', 'MONTHLY', 32, 64, 1, 'A100 40G', 200, 6999.0, 8640.0, true, 80, NOW(), NOW())
           ON CONFLICT DO NOTHING""",
    ]),

    ("8. 预置算力市场产品 (market_products)", [
        """INSERT INTO market_products (id, category, name, description, specs, price, price_unit, tags, sort_order, is_active,
               created_at, updated_at)
           VALUES
               (gen_random_uuid(), 'COMPUTE', 'RTX 3090 云主机',
                '24GB显存，适合模型微调、推理部署和AI开发',
                '{"gpu": "RTX 3090", "gpu_memory": "24GB", "cpu": "8核", "memory": "16GB", "disk": "50GB SSD", "bandwidth": "100Mbps"}',
                2.0, '元/小时',
                '["入门", "性价比"]', 10, true, NOW(), NOW()),

               (gen_random_uuid(), 'COMPUTE', 'RTX 4090 云主机',
                '24GB显存，Ada Lovelace架构，适合中等规模AI训练和推理',
                '{"gpu": "RTX 4090", "gpu_memory": "24GB", "cpu": "16核", "memory": "32GB", "disk": "100GB SSD", "bandwidth": "200Mbps"}',
                3.5, '元/小时',
                '["热门", "推荐"]', 20, true, NOW(), NOW()),

               (gen_random_uuid(), 'COMPUTE', 'A100 40G 云主机',
                '40GB HBM2e显存，专业级AI训练卡，支持NVLink',
                '{"gpu": "A100 40G", "gpu_memory": "40GB", "cpu": "32核", "memory": "64GB", "disk": "200GB SSD", "bandwidth": "500Mbps"}',
                12.0, '元/小时',
                '["专业", "训练"]', 30, true, NOW(), NOW()),

               (gen_random_uuid(), 'COMPUTE', 'A100 80G 云主机',
                '80GB HBM2e显存，大模型训练首选，支持NVLink互联',
                '{"gpu": "A100 80G", "gpu_memory": "80GB", "cpu": "64核", "memory": "128GB", "disk": "500GB SSD", "bandwidth": "1Gbps"}',
                18.0, '元/小时',
                '["旗舰", "大模型"]', 40, true, NOW(), NOW()),

               (gen_random_uuid(), 'COMPUTE', 'H100 80G 云主机',
                '80GB HBM3显存，Hopper架构，全球顶级AI计算卡',
                '{"gpu": "H100 80G", "gpu_memory": "80GB", "cpu": "64核", "memory": "256GB", "disk": "500GB NVMe", "bandwidth": "1Gbps"}',
                25.0, '元/小时',
                '["顶配", "旗舰"]', 50, true, NOW(), NOW()),

               (gen_random_uuid(), 'COMPUTE', 'V100 32G 云主机',
                '32GB HBM2显存，经典深度学习卡，稳定可靠',
                '{"gpu": "V100 32G", "gpu_memory": "32GB", "cpu": "16核", "memory": "64GB", "disk": "100GB SSD", "bandwidth": "200Mbps"}',
                8.0, '元/小时',
                '["经典", "稳定"]', 60, true, NOW(), NOW())
           ON CONFLICT DO NOTHING""",
    ]),

    ("8. 预置AI应用产品 (market_products)", [
        """INSERT INTO market_products (id, category, name, description, specs, price, price_unit, tags, sort_order, is_active,
               created_at, updated_at)
           VALUES
               (gen_random_uuid(), 'AI_APP', 'ChatGLM3 智能对话',
                '清华开源大语言模型，支持中英双语对话、代码生成、数学推理',
                '{"model": "ChatGLM3-6B", "parameters": "6B", "gpu_memory": "13GB", "min_gpu": "RTX 3090"}',
                0, '元/次',
                '["热门", "中文"]', 10, true, NOW(), NOW()),

               (gen_random_uuid(), 'AI_APP', 'Stable Diffusion XL',
                '高质量文生图模型，支持多种风格和LoRA扩展',
                '{"model": "SDXL 1.0", "parameters": "3.5B", "gpu_memory": "8GB", "min_gpu": "RTX 3090"}',
                0, '元/张',
                '["热门", "绘画"]', 20, true, NOW(), NOW()),

               (gen_random_uuid(), 'AI_APP', 'Whisper 语音识别',
                'OpenAI Whisper 大规模语音识别模型，支持99种语言',
                '{"model": "Whisper Large-V3", "parameters": "1.5B", "gpu_memory": "10GB", "min_gpu": "RTX 3090"}',
                0, '元/分钟',
                '["语音", "多语言"]', 30, true, NOW(), NOW()),

               (gen_random_uuid(), 'AI_APP', 'LLaMA 3 开源大模型',
                'Meta LLaMA 3 开源大语言模型，强大的通用对话能力',
                '{"model": "LLaMA-3-8B", "parameters": "8B", "gpu_memory": "16GB", "min_gpu": "RTX 4090"}',
                0, '元/次',
                '["开源", "通用"]', 40, true, NOW(), NOW()),

               (gen_random_uuid(), 'AI_APP', 'ComfyUI 工作流',
                '基于节点的AI图片工作流，支持自定义模型组合与批量处理',
                '{"model": "ComfyUI", "parameters": "N/A", "gpu_memory": "8GB", "min_gpu": "RTX 3090"}',
                0, '元/小时',
                '["绘画", "工作流"]', 50, true, NOW(), NOW())
           ON CONFLICT DO NOTHING""",
    ]),

    ("8. 预置应用镜像 (app_images)", [
        """INSERT INTO app_images (id, name, tag, category, description, image_url, size_gb, is_public, sort_order, status,
               created_at, updated_at)
           VALUES
               (gen_random_uuid(), 'Ubuntu 22.04', 'cuda12.2',
                'base', 'Ubuntu 22.04 + CUDA 12.2 + Python 3.10 基础环境',
                'nvcr.io/nvidia/cuda:12.2.0-devel-ubuntu22.04', 8.5, true, 10, 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'Ubuntu 22.04', 'cuda11.8',
                'base', 'Ubuntu 22.04 + CUDA 11.8 + Python 3.10 基础环境',
                'nvcr.io/nvidia/cuda:11.8.0-devel-ubuntu22.04', 7.8, true, 20, 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'PyTorch 2.2', 'cuda12.1-py310',
                'framework', 'PyTorch 2.2 + CUDA 12.1 + Python 3.10，含 torchvision 和 torchaudio',
                'nvcr.io/nvidia/pytorch:24.01-py3', 15.0, true, 30, 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'PyTorch 2.1', 'cuda11.8-py310',
                'framework', 'PyTorch 2.1 + CUDA 11.8 + Python 3.10，广泛兼容',
                'nvcr.io/nvidia/pytorch:23.10-py3', 14.2, true, 40, 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'TensorFlow 2.15', 'cuda12.2-py310',
                'framework', 'TensorFlow 2.15 + CUDA 12.2 + Python 3.10',
                'nvcr.io/nvidia/tensorflow:24.01-tf2-py3', 14.8, true, 50, 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'Jupyter Lab', 'cuda12.1-py310',
                'tool', 'Jupyter Lab + CUDA 12.1 + 常用数据科学工具包',
                'nvcr.io/nvidia/pytorch:24.01-py3', 15.0, true, 60, 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'ComfyUI', 'latest',
                'model', 'ComfyUI AI绘画工作流 + 预装 SDXL 基础模型',
                'registry.cn-hangzhou.aliyuncs.com/lmaicloud/comfyui:latest', 20.0, true, 70, 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'ChatGLM3', '6b-int4',
                'model', 'ChatGLM3-6B 量化版，低显存可用，支持中英双语对话',
                'registry.cn-hangzhou.aliyuncs.com/lmaicloud/chatglm3:6b-int4', 12.0, true, 80, 'ACTIVE', NOW(), NOW())
           ON CONFLICT DO NOTHING""",
    ]),

    ("8. 补齐种子数据时间戳", [
        "UPDATE resource_plans SET created_at = NOW(), updated_at = NOW() WHERE created_at IS NULL",
        "UPDATE market_products SET created_at = NOW(), updated_at = NOW() WHERE created_at IS NULL",
        "UPDATE app_images SET created_at = NOW(), updated_at = NOW() WHERE created_at IS NULL",
        "UPDATE images SET created_at = NOW(), updated_at = NOW() WHERE created_at IS NULL",
    ]),

    ("8. 预置容器镜像 (images)", [
        """INSERT INTO images (id, name, version, type, size, description, is_public, author, tags, status,
               created_at, updated_at)
           VALUES
               (gen_random_uuid(), 'Ubuntu CUDA', '12.2-22.04',
                'OFFICIAL', 8.5, 'Ubuntu 22.04 + CUDA 12.2 官方基础镜像', true, 'NVIDIA',
                '["CUDA", "Ubuntu", "基础"]', 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'Ubuntu CUDA', '11.8-22.04',
                'OFFICIAL', 7.8, 'Ubuntu 22.04 + CUDA 11.8 官方基础镜像', true, 'NVIDIA',
                '["CUDA", "Ubuntu", "基础"]', 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'PyTorch', '2.2-cu121',
                'OFFICIAL', 15.0, 'PyTorch 2.2 + CUDA 12.1 官方训练镜像', true, 'NVIDIA',
                '["PyTorch", "深度学习", "训练"]', 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'PyTorch', '2.1-cu118',
                'OFFICIAL', 14.2, 'PyTorch 2.1 + CUDA 11.8 官方训练镜像', true, 'NVIDIA',
                '["PyTorch", "深度学习", "兼容"]', 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'TensorFlow', '2.15-cu122',
                'OFFICIAL', 14.8, 'TensorFlow 2.15 + CUDA 12.2 官方训练镜像', true, 'NVIDIA',
                '["TensorFlow", "深度学习"]', 'ACTIVE', NOW(), NOW()),

               (gen_random_uuid(), 'Jupyter Lab', 'cu121-py310',
                'OFFICIAL', 15.0, 'Jupyter Lab 交互开发环境，含常用科学计算包', true, 'LMAICloud',
                '["Jupyter", "开发工具", "交互式"]', 'ACTIVE', NOW(), NOW())
           ON CONFLICT DO NOTHING""",
    ]),

    # ── 9. 按实际运行时长计费改造 ──────────────────────────────
    ("9. 计费流水表 billing_records", [
        """CREATE TABLE IF NOT EXISTS billing_records (
               id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
               user_id UUID NOT NULL REFERENCES ai_users(id),
               instance_id UUID REFERENCES instances(id),
               openclaw_instance_id UUID REFERENCES openclaw_instances(id),
               amount FLOAT NOT NULL,
               hourly_price FLOAT NOT NULL,
               duration_seconds INTEGER NOT NULL,
               period_start TIMESTAMP NOT NULL,
               period_end TIMESTAMP NOT NULL,
               description VARCHAR(200),
               created_at TIMESTAMP DEFAULT NOW()
           )""",

        "CREATE INDEX IF NOT EXISTS idx_billing_records_user ON billing_records(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_billing_records_instance ON billing_records(instance_id)",
    ]),

    ("9. Instance/OpenClaw 新增 last_billed_at", [
        "ALTER TABLE instances ADD COLUMN IF NOT EXISTS last_billed_at TIMESTAMP",
        "ALTER TABLE openclaw_instances ADD COLUMN IF NOT EXISTS last_billed_at TIMESTAMP",

        # 对已 RUNNING 的实例初始化 last_billed_at（从迁移时刻开始计费）
        # status 列是 PostgreSQL 枚举类型，需用 ::text 转换后比较
        "UPDATE instances SET last_billed_at = NOW() WHERE status::text = 'RUNNING' AND last_billed_at IS NULL",
        "UPDATE openclaw_instances SET last_billed_at = NOW() WHERE status::text = 'running' AND last_billed_at IS NULL",
    ]),

    # ── 10. BillingType 枚举补全 ─────────────────────────────────
    ("10. BillingType 枚举补全 (DAILY/WEEKLY/YEARLY)", [
        "ALTER TYPE billingtype ADD VALUE IF NOT EXISTS 'DAILY'",
        "ALTER TYPE billingtype ADD VALUE IF NOT EXISTS 'WEEKLY'",
        "ALTER TYPE billingtype ADD VALUE IF NOT EXISTS 'YEARLY'",
    ]),

    # ── 11. OpenClaw 实例计费字段 ─────────────────────────
    ("11. OpenClaw 实例计费字段 (billing_type/hourly_price/expired_at)", [
        # 直接使用 VARCHAR(20) 避免 billingtype 枚举值大小写不匹配问题
        "ALTER TABLE openclaw_instances ADD COLUMN IF NOT EXISTS billing_type VARCHAR(20) DEFAULT 'hourly'",

        "ALTER TABLE openclaw_instances ADD COLUMN IF NOT EXISTS hourly_price FLOAT DEFAULT 0.12",

        "ALTER TABLE openclaw_instances ADD COLUMN IF NOT EXISTS expired_at TIMESTAMP",

        "UPDATE openclaw_instances SET billing_type = 'hourly' WHERE billing_type IS NULL",
        "UPDATE openclaw_instances SET hourly_price = 0.12 WHERE hourly_price IS NULL",
    ]),

    # ── 12. billing_records 增加计费对象字段 ─────────────────────
    ("12. billing_records 增加计费对象字段 (instance_name/resource_type)", [
        "ALTER TABLE billing_records ADD COLUMN IF NOT EXISTS instance_name VARCHAR(100)",
        "ALTER TABLE billing_records ADD COLUMN IF NOT EXISTS resource_type VARCHAR(20)",

        # 回填历史数据：关联实例名称
        """UPDATE billing_records br SET resource_type = 'gpu', instance_name = i.name
           FROM instances i WHERE br.instance_id = i.id AND br.resource_type IS NULL""",
        """UPDATE billing_records br SET resource_type = 'openclaw', instance_name = oc.name
           FROM openclaw_instances oc WHERE br.openclaw_instance_id = oc.id AND br.resource_type IS NULL""",

        # 实例已删除的历史记录：根据外键补填类型，名称缺省
        """UPDATE billing_records SET resource_type = 'gpu', instance_name = '已释放实例'
           WHERE instance_id IS NOT NULL AND resource_type IS NULL""",
        """UPDATE billing_records SET resource_type = 'openclaw', instance_name = '已释放实例'
           WHERE openclaw_instance_id IS NOT NULL AND resource_type IS NULL""",
    ]),

    # ── 13. ai_users 增加实例配额字段 ─────────────────────────
    ("13. ai_users 增加 instance_quota 字段", [
        "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS instance_quota INTEGER DEFAULT 20",
        "UPDATE ai_users SET instance_quota = 20 WHERE instance_quota IS NULL",
    ]),

# ── 14. 审计日志支持登录失败记录 ─────────────────────
    ("14. 审计日志支持登录失败记录 (login_failed + user_id nullable)", [
        "ALTER TYPE auditaction ADD VALUE IF NOT EXISTS 'LOGIN_FAILED'",
        "ALTER TABLE audit_logs ALTER COLUMN user_id DROP NOT NULL",
    ]),

    # ── 15. OpenClaw 监控字段补全 ─────────────────────
    ("15. openclaw_model_keys 增加 check_message 字段", [
        "ALTER TABLE openclaw_model_keys ADD COLUMN IF NOT EXISTS check_message VARCHAR(200)",
    ]),

# ── 16. 用户手机号字段 ─────────────────────────
    ("16. ai_users 增加 phone 字段", [
        "ALTER TABLE ai_users ADD COLUMN IF NOT EXISTS phone VARCHAR(30)",
    ]),
]


# ============================================================
# 执行引擎
# ============================================================

async def run_migrations():
    total_steps = len(MIGRATIONS)
    success_count = 0
    skip_count = 0
    fail_count = 0

    print("=" * 60)
    print("  LMAICloud 统一数据库迁移")
    print("=" * 60)
    print()

    def is_alter_type_add_value(sql: str) -> bool:
        """ALTER TYPE ... ADD VALUE 必须在事务外执行 (PostgreSQL 限制)"""
        s = sql.strip().upper()
        return "ALTER TYPE" in s and "ADD VALUE" in s

    # ── Phase 1: ALTER TYPE ADD VALUE (必须 autocommit，PostgreSQL 不允许在事务内执行) ──
    alter_type_sqls = []
    for name, sqls in MIGRATIONS:
        for sql in sqls:
            if is_alter_type_add_value(sql):
                alter_type_sqls.append((name, sql))

    if alter_type_sqls:
        print("[Phase 1] ALTER TYPE ADD VALUE (asyncpg 直连, autocommit)")
        # asyncpg 默认 autocommit，不受 SQLAlchemy 事务管理影响
        db_url = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
        pg_conn = await asyncpg.connect(db_url)
        try:
            for step_name, sql in alter_type_sqls:
                short = sql.strip()[:72]
                try:
                    await pg_conn.execute(sql)
                    print(f"    ✓ {short}")
                    success_count += 1
                except Exception as e:
                    err_msg = str(e).lower()
                    if "already exists" in err_msg or "duplicate" in err_msg:
                        print(f"    ⊘ 跳过 (已存在): {short}")
                        skip_count += 1
                    else:
                        print(f"    ✗ 失败: {short}")
                        print(f"      错误: {e}")
                        fail_count += 1
        finally:
            await pg_conn.close()
        print()

    # ── Phase 2: 其余迁移 (事务模式) ──
    print("[Phase 2] 常规迁移 (事务模式)")
    async with engine.begin() as conn:
        for idx, (name, sqls) in enumerate(MIGRATIONS, 1):
            regular_sqls = [s for s in sqls if not is_alter_type_add_value(s)]
            if not regular_sqls:
                print(f"[{idx}/{total_steps}] {name}")
                print(f"    ⊘ 已在 Phase 1 执行")
                print()
                continue

            print(f"[{idx}/{total_steps}] {name}")
            step_ok = True

            for sql in regular_sqls:
                short = sql.strip().split("\n")[0][:72]
                # 使用 SAVEPOINT 隔离每条 SQL，避免单条失败毒化整个事务
                sp = await conn.begin_nested()
                try:
                    await conn.execute(text(sql))
                    await sp.commit()
                    print(f"    ✓ {short}")
                    success_count += 1
                except Exception as e:
                    await sp.rollback()
                    err_msg = str(e).lower()
                    # 常见幂等错误：已存在 / 重复 / 找不到列
                    if any(kw in err_msg for kw in (
                        "already exists", "duplicate", "does not exist",
                        "already has", "cannot drop",
                    )):
                        print(f"    ⊘ 跳过 (已存在): {short}")
                        skip_count += 1
                    else:
                        print(f"    ✗ 失败: {short}")
                        print(f"      错误: {e}")
                        fail_count += 1
                        step_ok = False

            status = "✓" if step_ok else "✗"
            print(f"    {status} 步骤完成")
            print()

    # 汇总
    print("=" * 60)
    print(f"  迁移完成!")
    print(f"  成功: {success_count}  跳过: {skip_count}  失败: {fail_count}")
    print("=" * 60)

    if fail_count > 0:
        print("\n有失败项，请检查上方错误信息后重试。")
        sys.exit(1)
    else:
        print("\n所有迁移已成功执行，数据库已就绪。")


if __name__ == "__main__":
    asyncio.run(run_migrations())
