# 大厂做法参考（Meta / Google / Tesla）与对本方案的取舍

把 Meta、Google/DORA、Google SRE、Tesla 公开的遥测、研发效能与隐私治理做法，
映射到这个 token 统计系统上——**哪些直接借鉴、哪些刻意不照搬**。

## 0. 最重要的一条：别做「个人监控式排行榜」

大厂衡量研发效能基本是**团队/聚合维度**，并对个人级数据做访问控制，原因是个人排行榜会触发
Goodhart 定律——一旦 token 数变成考核指标，就会被刷（无意义长上下文、空跑）。Meta 的隐私基建
更是把「按目的限制使用」作为硬约束（见下）。

**建议落到本方案：**
- 默认看板用**部门/团队榜**（`dashboard/leaderboard.sql` 已有部门榜）；个人级视图设为**受限访问 + 留痕**。
- 指标定位成**成本分摊 / 容量规划**，不是「谁写得多」的绩效。这点先和管理层对齐，比技术更关键。

## 1. 解耦的事件总线 ingest（Meta: Scribe）

Meta 的遥测先进 [Scribe](https://engineering.fb.com/2019/10/07/data-infrastructure/scribe/)（分布式缓冲队列，按
category 分区），再分流到下游；这样采集端和分析端**解耦、可缓冲、不丢数**。

**取舍**：当前我们是客户端直发 HTTP（`tokreport.py: post()`），起步够用。规模上来后把 sink 换成
**Kafka/Redpanda/NATS**——只改 `post()` 一处（见 ARCHITECTURE「sink 扩展缝」），收集端做消费者。

## 2. 热/冷分层存储（Meta: Scuba + Hive）

Meta 把数据 100% 写长期存储 [Hive]，按采样比写入实时内存库
[Scuba](https://www.vldb.org/pvldb/vol15/p3522-mo.pdf) 供 ad-hoc 切片。

**取舍**：我们的 `usage_daily` 是**按天预聚合的热表**（≈ Scuba 角色），看板查它很快。
若要审计/回溯，再加一张**原始事件冷表**（或直接落 Parquet/S3 ≈ Hive 角色）。我们按天聚合，
所以无需 Scuba 那种采样——量天然小。

## 3. 隐私按目的治理（Meta: PAI / Policy Zones / 数据血缘）

Meta 的 [Privacy Aware Infrastructure](https://engineering.fb.com/2024/08/27/security/privacy-aware-infrastructure-purpose-limitation-meta/)
用 Policy Zones 强制「数据只能用于声明的目的」，用
[数据血缘](https://engineering.fb.com/2025/01/22/security/how-meta-discovers-data-flows-via-lineage-at-scale/)
追踪流向，并用 PrivacyLib 在读写处统一埋点；隐私工作流分四步：理解数据→发现流向→执行策略→证明合规。

**取舍（强烈建议照做，因为我们涉及员工无感知采集）：**
- **目的限制**：给数据打 `purpose`（如 `cost_allocation`），声明用途与**保留期**，到期自动清理
  （见 `collector/retention.sql`）。
- **采集即最小化（PrivacyLib 思路）**：在**唯一入口** `/v1/usage/report` 做强校验——只接受 token
  计数/成本/模型/时间，结构上**根本没有 prompt/代码字段**，从源头杜绝越采。
- **可证明合规**：保留 ingest 审计、保留期任务、访问日志，作为审计证据。
- **四步法**对应到上线 checklist：先和安全/法务/HR 对齐目的与告知 → 明确采集字段 → 配置保留与访问控制 → 留痕。

## 4. 典型事件 schema + 版本化（Meta: 强类型 Logger）

Meta 的 logger 是强类型 schema。我们的「归一化 record」就是这个 schema（见
`agent/collectors/base.py`），已固定字段；演进时按版本管理、下游只认契约即可。

## 5. Google/DORA：把 AI 用量放进交付质量上下文

Google Cloud 的 Four Keys / DORA 做法不是“数谁更忙”，而是把交付系统拆成 throughput 与
instability 两组结果指标。映射到本项目：

| metric id | 指标族 | 大厂参考 | 本项目口径 |
|---|---|---|---|
| `delivery_quality` | 交付质量 | Google/DORA 的 change lead time、deployment frequency、failed deployment recovery time、change fail rate、deployment rework rate | 先在前端渲染指标槽位；后续从 CI/CD、发布和事故系统接入真实数据 |
| `reliability_budget` | 可靠性 / error budget | Google SRE 的 SLI/SLO/error budget | 采集端成功率、同步延迟、API 错误率、数据新鲜度 |
| `cost_efficiency` | 成本效率 | DORA dashboard 与 Meta 热表联看趋势 | token、成本、缓存、有效代码行一起看 |

这个映射的关键是：AI token 是输入/成本信号，不是产出本身；必须和 change lead time、
deployment frequency、error budget 等结果信号放在一起，才能判断工具是否真的改善工程系统。

## 6. Tesla：遥测最小化、可控共享与 fleet health

Tesla 的公开隐私说明把 vehicle / diagnostic / AI data 分层，强调 Data Sharing 可控、默认本地或匿名、
只在诊断、服务或安全事件等目的下使用更敏感数据。映射到本项目：

| metric id | 指标族 | 大厂参考 | 本项目口径 |
|---|---|---|---|
| `adoption_coverage` | 覆盖率 | fleet telemetry 先知道多少设备/功能已接入 | 活跃终端、接入终端、工具覆盖、部门覆盖 |
| `privacy_purpose` | 目的限制 | Tesla Data Sharing + Meta Policy Zones | 字段最小化、purpose 标签、保留期、访问留痕 |
| `collection_health` | 采集链路健康 | fleet/diagnostic telemetry 需要可诊断 | 最近上报时间、重试、幂等覆盖、同步作业状态 |

Tesla 给本项目的取舍是：公开默认数据必须是合成样例；真实企业适配器可以存在，但要配置化、可关闭、
可告知，并且只上传聚合计数，不上传 prompt、代码正文或屏幕内容。

## 7. 前端渲染的治理指标

`collector/dashboard.html` 的“Note：大厂治理指标”视图固定渲染以下指标族：

- `cost_efficiency`：成本效率，避免只看 token 总量。
- `adoption_coverage`：覆盖与采集健康，先看分母再解释趋势。
- `code_acceptance`：代码采纳与有效行，衡量产出而不是活动量。
- `delivery_quality`：Google/DORA 交付质量指标槽位。
- `reliability_budget`：Google SRE SLI/SLO/error budget。
- `privacy_purpose`：Meta Policy Zones 与 Tesla Data Sharing 对应的目的限制。
- `collection_health`：ingest 成功率、重试、数据新鲜度和去重健康。

---

一句话：**技术上借鉴 Scribe 的解耦 ingest、Scuba/Hive 的热冷分层、PAI 的按目的治理、
Google/DORA 的交付结果指标、Google SRE 的 error budget、Tesla 的 Data Sharing 与最小化遥测；
组织上刻意不照搬「个人排行榜」，默认团队维度 + 个人受限**——这才是大厂真正的做法。

**Sources:** [Scribe](https://engineering.fb.com/2019/10/07/data-infrastructure/scribe/) ·
[Scuba (VLDB)](https://www.vldb.org/pvldb/vol15/p3522-mo.pdf) ·
[PAI 目的限制](https://engineering.fb.com/2024/08/27/security/privacy-aware-infrastructure-purpose-limitation-meta/) ·
[数据血缘](https://engineering.fb.com/2025/01/22/security/how-meta-discovers-data-flows-via-lineage-at-scale/) ·
[Google Four Keys](https://cloud.google.com/blog/products/devops-sre/using-the-four-keys-to-measure-your-devops-performance) ·
[DORA metrics](https://dora.dev/guides/dora-metrics/) ·
[Google SRE SLO](https://sre.google/sre-book/service-level-objectives/) ·
[Tesla Privacy Notice](https://www.tesla.com/legal/privacy)
