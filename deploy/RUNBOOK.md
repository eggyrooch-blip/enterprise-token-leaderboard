# RUNBOOK — tokreport-collector 部署与运维

## 概览

将 `collector/dev_collector.py`（纯标准库 + SQLite）部署到远端 ELK 服务器，作为员工 Mac 飞连执行脚本的上报端点。

- **远端主机**: `it@collector.example.com` (k_bj_server_pm_elk_new, CentOS7)
- **监听端口**: 8090
- **进程管理**: systemd (unit: `tokreport-collector.service`)
- **数据库**: `/home/it/tokreport/tok.db` (SQLite)
- **部署目录**: `/home/it/tokreport/`

---

## 侦察结论（2026-06-08）

| 项目 | 结论 |
|------|------|
| systemd | 存在，版本 219（CentOS7 原生） |
| it 用户 sudo | **有限 sudo**：可无密码执行 `sudo systemctl`，但无法执行 `firewall-cmd` |
| firewalld | **未运行**（`not running`），端口无需通过 firewalld 放行 |
| iptables | 未单独侦察；若员工 Mac 无法访问 8090，检查 `sudo iptables -L INPUT -n` |
| 飞连可达性 | **HTTP 404** — TCP 连通正常，路径不存在属正常（token API 需 POST+鉴权）。**飞连从远端可达** |
| `/opt` 可写 | 否（root 所有）。改用 `/home/it/tokreport/` |
| python3 路径 | `/usr/bin/python3`，版本 3.6.8 |
| docker | 版本 1.13.1，过老，不使用 |
| 磁盘 | `/` 93% 使用，剩余 316G。SQLite DB 占用极小，无问题 |

---

## 为何不用 Docker

远端 Docker 版本 1.13.1（2017年），缺少 `--init`、健康检查、compose v2 等特性，且与现有 systemd 服务管理冲突。`dev_collector.py` 是纯标准库，无外部依赖，直接 `python3` 运行更简单可靠。

---

## 前置条件（主控操作）

1. **填写凭证**：复制模板并填入真实值：
   ```bash
   cp deploy/.env.example pipeline/.env
   # 编辑 pipeline/.env，填入：
   #   FEILIAN_ACCESS_KEY_ID
   #   FEILIAN_ACCESS_KEY_SECRET
   #   COLLECTOR_API_TOKENS（发给员工 Mac 的 Bearer token）
   ```

2. **确认 ssh 免密**：
   ```bash
   ssh -o BatchMode=yes it@collector.example.com 'echo ok'
   ```

---

## 部署步骤

### 1. 执行部署脚本（幂等，可重复）

```bash
bash deploy/deploy.sh
```

脚本完成后服务文件已就位，**但服务尚未启动**。

### 2. 启动服务（主控在远端执行）

```bash
ssh it@collector.example.com 'sudo systemctl start tokreport-collector'
ssh it@collector.example.com 'sudo systemctl status tokreport-collector --no-pager'
```

### 3. 冒烟测试

```bash
bash deploy/smoke.sh
```

### 4. 查看日志

```bash
ssh it@collector.example.com 'sudo journalctl -u tokreport-collector -n 50 --no-pager'
# 实时跟踪
ssh it@collector.example.com 'sudo journalctl -u tokreport-collector -f'
```

---

## 飞连可达性

从远端访问飞连 `https://mdm.example.com:8443` 的 TCP 层可达（HTTP 404 是正常的，因为 GET token API 需要 POST + 鉴权）。

collector 启动后，员工 Mac 上报序列号时，远端会调用飞连 API 反解身份。若飞连 API 返回错误，identity 解析降级为使用上报方提供的 email 字段，不影响数据入库。

---

## 防火墙注意事项

firewalld 未运行，无需放行。但若员工 Mac 无法 POST 到 8090，检查 iptables：

```bash
# 在远端（需要 root 或有 sudo 的账号）
sudo iptables -L INPUT -n | grep 8090
# 若无放行规则，添加：
sudo iptables -I INPUT -p tcp --dport 8090 -j ACCEPT
# 持久化（CentOS7）：
sudo service iptables save
```

**注意**：`it` 用户无 `sudo iptables` 权限，此操作需要 root 或其他有权限的账号执行。

---

## 回滚

```bash
# 停止服务
ssh it@collector.example.com 'sudo systemctl stop tokreport-collector'

# 禁用开机自启（可选）
ssh it@collector.example.com 'sudo systemctl disable tokreport-collector'

# 删除 unit 文件
ssh it@collector.example.com 'sudo rm /etc/systemd/system/tokreport-collector.service && sudo systemctl daemon-reload'

# 保留数据库（如需清空）：
# ssh it@collector.example.com 'rm /home/it/tokreport/tok.db'
```

---

## 员工 Mac 端配置

员工 Mac 上的飞连执行脚本需要将上报地址指向 `http://collector.example.com:8090`，并在请求头带上 `Authorization: Bearer <COLLECTOR_API_TOKENS 中的值>`。

---

## 磁盘监控

当前 `/` 93% 使用，剩余 316G。SQLite DB 按实际 token 用量增长，每条记录约 200 字节，1000 名员工每日上报一次约 200KB/天，无磁盘压力。若磁盘持续增长，优先排查其他服务（ELK 本身的 index 数据）。

---

## 文件清单

| 文件 | 用途 |
|------|------|
| `deploy/deploy.sh` | 主部署脚本（幂等） |
| `deploy/tokreport-collector.service` | systemd unit 模板 |
| `deploy/smoke.sh` | 部署后冒烟测试 |
| `deploy/.env.example` | 凭证模板（无真实值，可提交） |
| `pipeline/.env` | 真实凭证（gitignore，不提交） |
| `collector/dev_collector.py` | 收集端主程序 |
| `collector/feilian_client.py` | 飞连 API 客户端 |
| `collector/litellm_collector.py` | LiteLLM 网关 token 同步（个人榜 merge + agent 榜拆分） |
| `deploy/litellm-sync.service` | LiteLLM 同步 oneshot unit 模板 |
| `deploy/litellm-sync.timer` | LiteLLM 同步定时器（hourly） |

---

## LiteLLM 网关 token 同步（2026-06-08 新增）

把 LiteLLM 网关（`litellm.example.com`，391 把 key）的用量周期性灌进同一个 `tok.db`。

**归属规则（核心需求）**：
- **个人 key** → 按企业邮箱 merge 进个人榜（与该人的 Claude/Cursor 订阅 token 求和）。`source='litellm'`，dept 取 LiteLLM team 中文名。
- **agent key**（team alias=`agent`，team_id `f5395438…`）→ **不进个人榜**，单独走 `GET /v1/agent_leaderboard`，按 `key_alias` 排名。`source='litellm_agent'`，identity=`agent:<alias>`。
- 判定唯一依据：key 的 `team_id == agent team`。当前 agent team 有 0 把 key，机制就绪，榜单待 agent key 落入后自动出现。

**运行方式**：`litellm-sync.timer` 每个整点触发 `litellm_collector.py`（oneshot），与 collector 同宿主、纯标准库（py3.6.8）、直接写 `tok.db`，免 HTTP 自上报。

**幂等**：每次拉 `[LITELLM_HISTORY_START, today]` 全窗口，先 `DELETE source IN ('litellm','litellm_agent')` 再整批重写 → 连跑不翻倍（已验证 RUN1==RUN2）。

**前置**：`pipeline/.env` 里需含 `LITELLM_BASE_URL` + `LITELLM_MASTER_KEY`（与 `ai-gateway-onboard/.env` 同一把只读 master key）。`deploy.sh` 已自动安装 service+timer 并 `enable --now`。

**手动运维**：
```bash
ssh it@collector.example.com 'sudo systemctl start litellm-sync.service'   # 立即跑一次
ssh it@collector.example.com 'sudo journalctl -u litellm-sync -n 30 --no-pager'  # 看日志
ssh it@collector.example.com 'sudo systemctl list-timers litellm-sync.timer'     # 看下次触发
# 只读演练（本机，不写库）：
LITELLM_BASE_URL=... LITELLM_MASTER_KEY=... python3 collector/litellm_collector.py --dry-run
```

---

## 飞书订阅名单同步（2026-06-12 新增）

每天从飞书表 `WuK7sLkIthIn2Htrz2BcIiipnEb` 读 4 个名单 tab，**以最新表单为准整表覆盖**
落库 `subscriptions` + `subscriptions_unresolved` 两张表。表里新增的人当天获得订阅徽章并
计订阅费，被删的人当天摘掉徽章并停止计费（`清退` tab 不读）。

**名单与计费**：
- Codex `aGseou`（注册邮箱多为 gmail，用「飞书实名」反查 `people` 表得企业邮箱）— $25/月
- Claude `6SIHS`（身份 = `<飞书 user_id>@keep.com`；备注含「Premium 席位」→ $100，否则 $25）
- Cursor `KvJN7D`（表内即 @keep.com）— $40/月
- Windsurf `fl4xUJ`（表内即 @keep.com）— $30/月
- Codex 解析不到企业邮箱或重名歧义的人**不静默丢弃**，落 `subscriptions_unresolved`
  （reason=`no_match`|`ambiguous`），看板治理区显示「订阅名单未归位 N 人」待人工补映射。

**归属与个人榜「公司实付」**：个人榜成本列改为公司实付 = 网关实销
（`usage_daily.source in ('api','litellm')`）+ Σ每席位 月费 ×（查询窗口∩席位 [开通,删除] 区间
的按天摊销系数：逐自然月「重叠天数/该月总天数」累加）。日均模式下成本折算仅个人榜。
订阅制工具 token 量照常展示，但不再按 API 牌价折算成成本。

**运行方式**：`subscriptions-sync.timer` 每天 03:30 触发 `subscriptions_sync.py`（oneshot），
与 collector 同宿主、纯标准库（py3.6.8）、直接写 `tok.db`，免 HTTP 自上报、不依赖孙可 Mac。

**幂等**：每次跑都在单事务内 `DELETE` 两表全部行再整批 `INSERT OR REPLACE` 重写 →
连跑两次行集不变。

**前置**：`pipeline/.env` 里需含 `FEISHU_APP_ID` + `FEISHU_APP_SECRET`（飞书 bot 应用凭证，
从 env 读，**绝不落代码/文档**；已验证 bot 身份可读该表全部 tab）。`deploy.sh` 安装 service+timer
时同步 `enable --now`。

**手动运维**：
```bash
ssh it@collector.example.com 'sudo systemctl start subscriptions-sync.service'   # 立即跑一次
ssh it@collector.example.com 'sudo journalctl -u subscriptions-sync -n 30 --no-pager'  # 看日志
ssh it@collector.example.com 'sudo systemctl list-timers subscriptions-sync.timer'     # 看下次触发
# 只读演练（本机，不写库，打印 4 tab 解析人数 + unresolved 计数）：
FEISHU_APP_ID=... FEISHU_APP_SECRET=... python3 collector/subscriptions_sync.py --dry-run
```

## 订阅数据与 Postgres 路径的边界（如实说明）

`subscriptions_sync.py` 当前只写 SQLite（`DEV_DB`，即生产部署的 `dev_collector` 路径,
服务器 `~/tokreport/tok.db`）。FastAPI/Postgres 版 `app.py` 实现了**相同的计算逻辑**
（按席位区间摊销/徽章聚合/闲置治理,由 tests/ 断言两端一致）,但其 `subscriptions`
表需要使用方自行灌数——Postgres 写入器是后续工作,当前未实现。生产事实来源以
SQLite 部署为准。

## 飞书通讯录同步（组织架构真源）

**运行方式**：`feishu-directory-sync.timer` 每天 02:10 触发
`feishu_directory_sync.py`（oneshot），与 collector 同宿主、纯标准库、直接写
`tok.db`。它写入/更新 `feishu_users`、`departments`、`department_attributions`、
`people`、`roles`，使看板权限、部门归属、负责人范围以飞书通讯录为准。

**前置**：`pipeline/.env` 里需含 `FEISHU_APP_ID` + `FEISHU_APP_SECRET`，同一 bot
必须开启 contact-read 通讯录读取权限，并对所有需要统计/授权的部门有可见范围。
`FEISHU_ROOT_DEPT` 默认 `0`；`AUTH_ADMIN_EMAILS` 用于补充管理员 allowlist，
`sunke@keep.com` 仍由代码固定为超管兜底。管理员确认业务外包归并后，可在 `.env`
设置 `FEISHU_DEPT_ATTRIBUTION_OVERRIDES=/path/to/department-overrides.json`；JSON 支持
`{"合作商/W/供应商(SPxxxxxx)": {"target_dept_path": "真实业务部门路径"}}`。

**手动运维**：
```bash
ssh it@collector.example.com 'sudo systemctl start feishu-directory-sync.service'   # 立即跑一次
ssh it@collector.example.com 'sudo journalctl -u feishu-directory-sync -n 80 --no-pager'  # 看日志
ssh it@collector.example.com 'sudo systemctl list-timers feishu-directory-sync.timer'     # 看下次触发
# 只读演练（本机，不写库，打印部门/用户/外包归因/可见性告警）：
FEISHU_APP_ID=... FEISHU_APP_SECRET=... python3 collector/feishu_directory_sync.py --dry-run --db /tmp/tok.db
```

**上线门槛**：dry-run/正式输出会打印 `attribution_counts_by_rule`、`manual_overrides`、
`resolved_business_outsourcing_rate` 和 `production_enablement_blocked`。低覆盖时目录、
人员、角色和复核候选仍会同步，但未确认的业务外包 roll-up 不会启用；明确接受低覆盖后才设置
`ALLOW_LOW_FEISHU_ATTRIBUTION_COVERAGE=1`，或手工运行时加 `--allow-low-coverage`。
