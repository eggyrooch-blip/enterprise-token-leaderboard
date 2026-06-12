-- 统一用量表：订阅制(客户端 tokscale)与 API(LiteLLM) 两路数据都落在这里。
-- 通过主键做幂等 upsert：同一天/同人/同来源/同工具/同模型只保留一行，
-- 客户端每次重传最近 N 天都会覆盖，离线补传/重复跑都不会重复计数。

CREATE TABLE IF NOT EXISTS usage_daily (
    email               TEXT          NOT NULL,
    dept                TEXT          NOT NULL DEFAULT 'unknown',
    usage_date          DATE          NOT NULL,
    source              TEXT          NOT NULL,            -- 'subscription' | 'api'
    tool                TEXT          NOT NULL,            -- claude_code | codex | cursor | gemini_cli | ...
    model               TEXT          NOT NULL DEFAULT 'unknown',
    input_tokens        BIGINT        NOT NULL DEFAULT 0,
    output_tokens       BIGINT        NOT NULL DEFAULT 0,
    cache_read_tokens   BIGINT        NOT NULL DEFAULT 0,
    cache_write_tokens  BIGINT        NOT NULL DEFAULT 0,
    total_tokens        BIGINT        NOT NULL DEFAULT 0,
    cost_usd            NUMERIC(14,6) NOT NULL DEFAULT 0,
    -- 目的限制(借鉴 Meta PAI)：声明数据用途，便于按目的治理/审计。
    purpose             TEXT          NOT NULL DEFAULT 'cost_allocation',
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (email, usage_date, source, tool, model)
);

CREATE INDEX IF NOT EXISTS idx_usage_daily_date   ON usage_daily (usage_date);
CREATE INDEX IF NOT EXISTS idx_usage_daily_email  ON usage_daily (email);
CREATE INDEX IF NOT EXISTS idx_usage_daily_dept   ON usage_daily (dept);
CREATE INDEX IF NOT EXISTS idx_usage_daily_source ON usage_daily (source);

-- 第二指标族：代码产出（采纳率 / 有效代码行数）。与 usage_daily 同样的人/天/工具维度，
-- 但来源不同：Cursor Admin API、Claude Code OTEL、git 存活分析等，各自 upsert 进来。
-- 采纳率不入库存原始值，查询时按 accepted/suggested 现算（避免存冗余派生列）。
CREATE TABLE IF NOT EXISTS code_daily (
    email               TEXT          NOT NULL,
    dept                TEXT          NOT NULL DEFAULT 'unknown',
    usage_date          DATE          NOT NULL,
    source              TEXT          NOT NULL,            -- 'cursor' | 'claude_code' | 'git' | ...
    tool                TEXT          NOT NULL,
    lines_suggested     BIGINT        NOT NULL DEFAULT 0,  -- 建议/展示的行数(分母)
    lines_accepted      BIGINT        NOT NULL DEFAULT 0,  -- 被采纳的行数(分子) = 有效代码行
    lines_added         BIGINT        NOT NULL DEFAULT 0,
    lines_removed       BIGINT        NOT NULL DEFAULT 0,
    suggestions_shown   BIGINT        NOT NULL DEFAULT 0,
    suggestions_accepted BIGINT       NOT NULL DEFAULT 0,
    commits             BIGINT        NOT NULL DEFAULT 0,
    lines_surviving     BIGINT        NOT NULL DEFAULT 0,  -- 选填：N 天后仍存活的行(git churn 分析)
    purpose             TEXT          NOT NULL DEFAULT 'productivity_insight',
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (email, usage_date, source, tool)
);
CREATE INDEX IF NOT EXISTS idx_code_daily_date ON code_daily (usage_date);
CREATE INDEX IF NOT EXISTS idx_code_daily_dept ON code_daily (dept);

-- 设备 -> 员工映射（可选）：如果你想由收集端而不是客户端来定身份，
-- 飞连下发设备清单时写到这里，上报只带 device_id，收集端 JOIN 出 email。
CREATE TABLE IF NOT EXISTS device_identity (
    device_id   TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    dept        TEXT NOT NULL DEFAULT 'unknown',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 付费订阅名单：每日从飞书 4 个名单 tab 整表覆盖落库（subscriptions_sync.py）。
-- 以最新表单为唯一事实：表里新增的人当天获得徽章+计订阅费，被删的人当天摘掉。
-- 个人榜「公司实付」= 网关实销(usage_daily.source in api/litellm) + 本表月费×覆盖月数。
-- 订阅徽章(个人榜姓名后)与月费明细均取自本表。tool/tier 取值见列注释。
CREATE TABLE IF NOT EXISTS subscriptions (
    email           TEXT          NOT NULL,
    tool            TEXT          NOT NULL,                       -- 'codex'|'claude'|'cursor'|'windsurf'
    tier            TEXT          NOT NULL DEFAULT 'standard',    -- 'standard'|'premium'（同人同工具多账号取最高档）
    monthly_fee_usd NUMERIC(10,2) NOT NULL DEFAULT 0,             -- 同人同工具多坐席的月费之和（已按 seats 聚合）
    seats           INTEGER       NOT NULL DEFAULT 1,             -- 同人同工具的账号数；单价×seats 已并入 monthly_fee_usd
    display_name    TEXT          NOT NULL DEFAULT '',
    dept            TEXT          NOT NULL DEFAULT '',
    synced_at       TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (email, tool)
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_email ON subscriptions (email);

-- 订阅名单未归位行：名单里解析不到企业邮箱的人（Codex gmail 反查 people 失败/重名歧义），
-- 不静默丢弃，落本表，看板治理区显示「订阅名单未归位 N 人」待人工补映射。
CREATE TABLE IF NOT EXISTS subscriptions_unresolved (
    tool         TEXT        NOT NULL,
    display_name TEXT        NOT NULL DEFAULT '',
    raw_email    TEXT        NOT NULL DEFAULT '',
    dept         TEXT        NOT NULL DEFAULT '',
    reason       TEXT        NOT NULL,                            -- 'no_match'|'ambiguous'
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
