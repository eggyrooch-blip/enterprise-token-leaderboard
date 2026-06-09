-- 飞书 AI 权益用量 —— 独立三表,绝不与 usage_daily(token)加总(点≠token)。
-- 由 app.py 启动时加载(在 schema.sql 之后),幂等。来源:飞书后台内部接口快照。

-- 1) 全员逐人:某计费周期内每人每功能消耗的「点数」。一周期一快照,按主键 upsert 覆盖。
CREATE TABLE IF NOT EXISTS feishu_ai_member (
    email         TEXT          NOT NULL,            -- 工号@yourcompany.com(externalID 拼接)
    name          TEXT          NOT NULL DEFAULT '',
    dept          TEXT          NOT NULL DEFAULT 'unknown',
    feature_key   TEXT          NOT NULL,            -- AI_credits | aily_credits
    credits       NUMERIC(18,4) NOT NULL DEFAULT 0,  -- 点数
    period_start  DATE          NOT NULL,
    period_end    DATE          NOT NULL,
    avatar        TEXT          NOT NULL DEFAULT '',
    entity_id     TEXT          NOT NULL DEFAULT '',  -- 飞书 entityID(稳定）
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (email, feature_key, period_start)
);
CREATE INDEX IF NOT EXISTS idx_feishu_member_period ON feishu_ai_member (period_start);
CREATE INDEX IF NOT EXISTS idx_feishu_member_dept   ON feishu_ai_member (dept);

-- 2) 额度盘:每个 featureKey 的总额度/已用/剩余(企业级),一周期一行。
CREATE TABLE IF NOT EXISTS feishu_ai_quota (
    feature_key   TEXT          NOT NULL,            -- AI_credits | aily_credits
    quota         NUMERIC(18,4) NOT NULL DEFAULT 0,  -- 共计点数
    used          NUMERIC(18,4) NOT NULL DEFAULT 0,
    remain        NUMERIC(18,4) NOT NULL DEFAULT 0,
    period_start  DATE          NOT NULL,
    period_end    DATE          NOT NULL,
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (feature_key, period_start)
);

-- 3) 趋势:企业级按天、按功能(bizType)的点数 + 当日用量人数。这是真·日粒度时序。
CREATE TABLE IF NOT EXISTS feishu_ai_trend (
    usage_date    DATE          NOT NULL,
    biz_type      TEXT          NOT NULL,            -- bizType id(1/2/3/.../15)
    biz_name      TEXT          NOT NULL DEFAULT '', -- 功能名(知识问答/智能纪要/aily...)
    credits       NUMERIC(18,4) NOT NULL DEFAULT 0,
    user_count    INTEGER       NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (usage_date, biz_type)
);
CREATE INDEX IF NOT EXISTS idx_feishu_trend_date ON feishu_ai_trend (usage_date);
