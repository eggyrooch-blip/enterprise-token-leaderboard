# 全链路交付 · 公开默认版

这个仓库的公开默认路径是端到端跑通「采集 → 身份归属 → 聚合存储 → 治理看板」。
私有 MDM、SSO、网关、员工目录都以适配器方式接入；公开样例只使用 `example.com`
和合成组织数据。

## 链路

```
本地 agent 日志 / tokscale JSON ─┐
AI 网关用量 / LiteLLM 同步 ──────┼─> collector ─> SQLite/Postgres ─> dashboard
代码采纳 / Cursor Admin API ─────┘
```

- **采集**：本地 agent 只上传聚合 token 计数、成本、模型、日期，不上传 prompt 或代码内容。
- **身份**：默认用配置或 git email；企业可替换为 SSO、MDM 或设备清单 JOIN。
- **覆盖率分母**：公开版用合成 demo；企业部署可从 MDM/资产系统同步活跃终端数。
- **呈现**：`collector/dashboard.html` 是中性企业治理看板，包含个人/部门/工具/模型/Agent 榜和“大厂治理指标”。

## 本地运行

```bash
cd collector
COLLECTOR_API_TOKENS=devtoken DEV_DB=/tmp/tok-demo.db PORT=8090 python3 dev_collector.py
open http://localhost:8090/
```

如需灌入演示数据，可运行：

```bash
cd collector
COLLECTOR_URL=http://localhost:8088 COLLECTOR_TOKEN=devtoken python3 seed_demo.py
```

## 企业接入步骤

1. 部署 collector 到内网地址，例如 `https://collector.example.com`。
2. 下发 agent 安装脚本，按系统使用独立入口：macOS 用 `agent/mdm_bootstrap.sh`（LaunchAgent + `/tokreport.sh`），Windows 用 `agent/mdm_bootstrap_windows.ps1 -Collector https://<collector> -Token <token>`（Task Scheduler / Scheduled Task + `/tokreport.ps1`）。
3. 选择身份来源：配置文件、git email、SSO、MDM 设备清单或自定义 `device_identity` 表。
4. 可选接入 LiteLLM、Cursor Admin API、CI/CD、事故系统与审计日志，补齐治理指标。
5. 上线前完成员工告知、安全/法务审阅、保留期配置和访问控制。

## 开源边界

- 公开默认数据必须是合成数据。
- 私有域名、私有 IP、员工邮箱、真实姓名、生产 SSH 地址不得提交。
- 品牌私有页面、字体和 logo 不作为默认公开资产发布。
- 使用 `python3 scripts/open_source_guard.py` 做提交前检查。
