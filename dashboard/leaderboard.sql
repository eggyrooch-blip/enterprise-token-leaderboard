-- 排行榜查询合集（直接贴进 Grafana/Metabase 面板即可）。
-- 两路数据已统一在 usage_daily：source='api'(LiteLLM) + source='subscription'(客户端)。

-- 1) 总榜：最近 30 天，按人，含两路拆分
SELECT
    email,
    dept,
    SUM(total_tokens)                                          AS total_tokens,
    SUM(total_tokens) FILTER (WHERE source = 'api')            AS api_tokens,
    SUM(total_tokens) FILTER (WHERE source = 'subscription')   AS sub_tokens,
    ROUND(SUM(cost_usd), 2)                                    AS cost_usd
FROM usage_daily
WHERE usage_date >= current_date - 29
GROUP BY email, dept
ORDER BY total_tokens DESC
LIMIT 100;

-- 2) 部门榜
SELECT dept,
       SUM(total_tokens) AS total_tokens,
       ROUND(SUM(cost_usd), 2) AS cost_usd
FROM usage_daily
WHERE usage_date >= current_date - 29
GROUP BY dept
ORDER BY total_tokens DESC;

-- 3) 工具维度（claude_code / codex / cursor / litellm ...）
SELECT tool,
       SUM(total_tokens) AS total_tokens
FROM usage_daily
WHERE usage_date >= current_date - 29
GROUP BY tool
ORDER BY total_tokens DESC;

-- 4) 单人每日趋势（Grafana 用 $email 变量）
SELECT usage_date AS time, source, SUM(total_tokens) AS tokens
FROM usage_daily
WHERE email = '$email' AND usage_date >= current_date - 89
GROUP BY usage_date, source
ORDER BY usage_date;

-- ============ 第二指标族：代码产出（采纳率 / 有效代码行数）============

-- 5) 有效代码行数榜（被采纳的行 = 真正落地的产出），最近 30 天按部门
SELECT dept,
       SUM(lines_accepted)  AS effective_lines,
       SUM(lines_surviving) AS surviving_lines   -- 选填：N 天后仍存活
FROM code_daily
WHERE usage_date >= current_date - 29
GROUP BY dept
ORDER BY effective_lines DESC;

-- 6) AI 代码采纳率（按工具）：accepted / suggested。低采纳率=工具水土不服或需培训
SELECT tool,
       SUM(lines_accepted)   AS accepted,
       SUM(lines_suggested)  AS suggested,
       ROUND(100.0 * SUM(lines_accepted) / NULLIF(SUM(lines_suggested), 0), 1) AS accept_rate_pct
FROM code_daily
WHERE usage_date >= current_date - 29
GROUP BY tool
ORDER BY accepted DESC;

-- 7) 性价比视角：每万 token 产出多少有效代码行（关联两张表，识别“烧 token 不出活”）
SELECT u.email,
       SUM(c.lines_accepted)                                              AS effective_lines,
       SUM(u.total_tokens)                                                AS tokens,
       ROUND(SUM(c.lines_accepted) / NULLIF(SUM(u.total_tokens), 0) * 10000, 2) AS lines_per_10k_tokens
FROM usage_daily u
LEFT JOIN code_daily c ON c.email = u.email AND c.usage_date = u.usage_date
WHERE u.usage_date >= current_date - 29
GROUP BY u.email
ORDER BY effective_lines DESC NULLS LAST;
