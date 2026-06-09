# 架构与扩展点

设计目标：**任意企业可复用**（不绑定飞连/MDM）、**员工弱感知/无感知**、**强扩展性**。
核心做法是把每个可变部分都收敛成一个清晰的「扩展缝」，新增能力只动一处。

## ADR log

### 2026-06-08 - 生产品牌资产使用通用文件名，治理指标由 collector 实时计算

- **Decision:** dashboard 只引用 `/assets/company-logo.svg`，缺失时回退中性 `ET`；生产部署把已有真实 logo 映射到这个通用文件名。治理 tab 从 `/v1/governance_metrics` 获取当前 SQLite 可计算值。
- **Why:** 开源仓库不能提交 Keep logo/font/域名等私有资产，但生产看板不能因此丢品牌。治理指标也必须区分当前能算的数据和还需外部系统接入的数据。
- **Consequence:** 开源默认环境保持中性和可运行；生产环境不改 `.env` 即可恢复 logo。`cost_efficiency`、`adoption_coverage`、`privacy_purpose` 可直接计算，`code_acceptance`、`reliability_budget`、`collection_health` 部分可算，`delivery_quality` 待 CI/CD 与事故系统接入。

```
[ 采集源 collectors ]      [ 身份 identity ]
  tokscale ─┐                git email / SSO / MDM / 兜底
  claude_code┤                       │
  (你的新源) ┘                       ▼
        └──► 归一化 record ──► [ sink ] ──► [ collector API ] ──► [ 存储 ] ──► [ 看板 ]
             (统一契约)         HTTP/DB       /v1/usage/report     Postgres     Grafana
```

## 0.5 两个指标族，同一套架构

- **token 量**（花了多少）→ 表 `usage_daily`，入口 `/v1/usage/report`，来源 tokscale / LiteLLM。
- **代码产出**（采纳率 / 有效代码行）→ 表 `code_daily`，入口 `/v1/code/report`，来源 Cursor Admin API /
  Claude Code OTEL / git 存活分析。详见 [`CODE-METRICS.md`](CODE-METRICS.md)。
- **治理 / 交付指标**（是否可控、可靠、有效）→ 前端先渲染 `cost_efficiency`、
  `adoption_coverage`、`code_acceptance`、`delivery_quality`、`reliability_budget`、
  `privacy_purpose`、`collection_health` 七个指标槽位；后续真实数据来自 CI/CD、事故系统、
  采集端心跳、访问审计和保留期任务。

三类指标共用同样的扩展缝（来源可插拔、source 自由标签、身份/sink/存储/看板可替换），
所以「再加一类指标」也只是再加一张宽表 + 一个 ingest 入口，下游照旧。

治理指标的原则来自 `BIG-TECH-PATTERNS.md`：Meta Policy Zones / purpose limitation、
Google/DORA delivery quality、Google SRE error budget、Tesla Data Sharing 与最小化遥测。
它们默认按团队/系统维度展示，不作为个人绩效评分。

## 1. 归一化 record —— 整个系统的稳定契约

所有来源最终都产出同一种 record（见 `agent/collectors/base.py` 注释）：
`usage_date, source, tool, model, input/output/cache_* _tokens, total_tokens, cost_usd`。
收集端、表结构、看板都只依赖它。**只要新来源能产出这个形状，就能接入，无需改下游。**

## 2. 采集源 collectors（可插拔）

- 接口：`agent/collectors/base.py: UsageCollector`（`available()` + `collect(day)`）。
- 已带：`tokscale`（一把覆盖 25+ 工具）、`claude_code`（零依赖，参考实现）。
- **加一个新工具**：写 `xxx_collector.py` 实现接口 → 在 `collectors/__init__.py: REGISTRY` 登记一行 →
  配置 `COLLECTORS=...,xxx`。例如 codex/gemini 的直读、或读公司其它工具。
- 客户端按机器能力自动跳过不具备条件的源（`available()`），所以一份配置可全公司通发。

## 3. 身份 identity（支持零输入）

`agent/identity.py: resolve()` 按优先级：环境变量 → 配置(MDM下发) → `git config user.email` → 登录名@域名。
- **有 MDM**：下发 `EMPLOYEE_EMAIL`，强归属。
- **无 MDM**：留空即自动用 git email，员工零操作。
- 想换成 SSO/OIDC、或上报 `device_id` 由收集端 `device_identity` 表 JOIN —— 只改这一个文件。

## 4. source 维度（来源可任意扩展）

收集端 `usage_daily.source` 是自由标签（`^[a-z0-9_]{1,32}$`）：`subscription`/`api`/
`cursor_admin`/`bedrock`/…。新增一路服务端采集（如 Cursor Admin API、Bedrock CloudWatch）
只要把数据 upsert 进 `usage_daily` 用新 source 即可，看板自动多一类，无需改 schema。

## 5. sink / 存储 / 看板

- sink 当前是 HTTP（`/v1/usage/report`）；要换 Kafka/直写 DB/S3，只改 `tokreport.py: post()`。
- 存储是 Postgres，表是通用宽表；量级上来可平替 ClickHouse，契约不变。
- 看板是 Grafana（SQL 面板），换 Metabase/Superset 同理，读同一张表。

## 部署模式（任选，互不排斥）

| 模式 | 适用 | 入口 |
|---|---|---|
| MDM / 飞连 | 有统一终端管理 | `agent/install.sh`（root，按设备下发身份） |
| 无 MDM 自助 | 没有 MDM 的企业 | `agent/bootstrap.sh`（`curl\|bash`，免 root，git email 自动归属） |
| 随开发环境捆绑 | 已有 dotfiles/装机脚本 | 把 bootstrap 步骤并进现有装机流程 |

## 弱感知 / 无感知

- 客户端是 LaunchAgent 后台静默跑，无弹窗、无交互，日志写 `/tmp/tokreport.*`。
- 身份自动解析 → 员工无需任何录入。
- 合规提醒：无感知采集涉及员工数据，**上线前必须与安全/法务/HR 对齐并按当地法规告知**；
  只采 token 计数、绝不采 prompt/代码，是降低合规风险的基本前提。
