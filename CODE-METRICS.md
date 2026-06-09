# 第二指标族：代码采纳率 / 有效代码行数

token 量衡量「花了多少」，这一族衡量「产出了多少」。**数据来源完全不同**，不在 token 链路里，
各工具单独取，统一 upsert 进 `code_daily`（同人/天/工具维度），看板里和 token 榜并排看。

> 复用同一套扩展架构：新增一个代码指标来源 = 写一个 sync/collector，把数据按
> `/v1/code/report` 的契约或直接 upsert 进 `code_daily`，**不动表结构、不动下游**。

## 各工具的数据来源

| 工具 | 来源 | 取到什么 | 采集方式 |
|---|---|---|---|
| **Cursor** | [Admin API `/teams/daily-usage-data`](https://docs.cursor.com/en/account/teams/admin-api) | 每人每天 `linesAdded/Deleted`、`acceptedLines*`、`tabsShown/Accepted` → 采纳率、有效行 | `collector/cursor_admin_sync.py`（已实现，需团队 admin key） |
| **Claude Code** | [OpenTelemetry](https://code.claude.com/docs/en/agent-sdk/observability) | `claude_code.lines_of_code.count`(增删行)、`claude_code.code_edit_tool.decision`(accept/reject)、`commit_count` | OTEL 导出（见下） |
| **Codex / 其它** | 多数无原生采纳信号 | 退化为「提交/存活行」 | git 存活分析（见下） |

### Cursor（已实现）

```bash
DATABASE_URL=... CURSOR_API_KEY=<team-admin-key> python collector/cursor_admin_sync.py
```
采纳率 = `lines_accepted / lines_suggested`，有效代码行 = `lines_accepted`（看板查询已带）。

### Claude Code（OTEL）

每台机器开启遥测，把指标推到你的 OTEL Collector：
```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.example.com
```
> 这两条 env 可由飞连/MDM 或 bootstrap 一并下发（和身份解析一样零感知）。
> OTEL Collector 侧把 `lines_of_code.count` / `code_edit_tool.decision` 落到 `code_daily`
> （`source='claude_code'`，按 resource 上的用户属性归属到 email），或先进 Prometheus 再桥接。
> 这是官方、最可靠的口径；若不想上 OTEL，也可照 `agent/collectors/` 模式写一个读本地
> JSONL 的 `claude_code_code` collector（参考实现，留作扩展点）。

### 有效/存活代码行（git churn，工具无关）

「生成有效的代码行数」最稳的口径是**存活行**：AI 写入的行，N 天后仍留在仓库里（扣掉很快被改回的）。
做法：定时对目标仓库跑 `git log --numstat` + blame 存活分析，按作者/天写入 `code_daily.lines_surviving`。
这能识别「采纳了但很快被推翻」的虚高产出，是比裸 LOC 更真实的指标。

## ⚠️ 强烈建议：把 token 和产出放一起看，并保持团队维度

- 单看 LOC/采纳率会被刷（多生成、凑行数）——**Goodhart 定律**。看板已提供「每万 token 产出有效行数」
  的关联视图（`leaderboard.sql` 第 7 条），识别「烧 token 不出活」与「凑行数」两头。
- 沿用大厂做法（见 `BIG-TECH-PATTERNS.md`）：**默认部门/团队榜**，个人级受限；指标定位为
  效能洞察与成本分摊，不作个人绩效考核。代码指标比 token 更敏感，这条更要守住。
- 合规：只采聚合的行数/采纳计数，**不采任何代码内容**；上线前与安全/法务/HR 对齐。
