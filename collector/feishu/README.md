# 飞书 AI 权益用量采集器

把飞书管理后台 `your-tenant.feishu.cn/admin/aibilling`(AI 权益用量)的数据,**可持续、无人值守**地采集进现有 Token 看板,作为**独立板块**展示(单位是「点」，不与 AI 编程 token 加总)。

## 为什么这么做(关键约束)

飞书**没有**官方 AI 权益用量 API(`tenant_access_token` 那套只管 API 调用次数计费)。数据只在管理后台,且:

- 纯 cookie 直连接口 → `401`(要 SPA 的 `x-csrf-token` 头)
- 在页面里注入脚本抓数据 → 触发飞书**防篡改**

所以方案是:**拷一份你日常 Chrome 里登录了飞书的 profile → 用它带调试端口起一个 headless Chrome → Playwright 经 CDP 连上去**,让页面用自己的鉴权请求,在网络层旁路抓响应(对防篡改不可见);全员明细则借一次 UI 查询「暖」会话并抓下 `x-csrf-token` 头,再用该头 `ctx.request.post` 翻页拿全。归一化后 HTTPS 上报看板,**绝不直连 DB**。

> Chrome 136+ 禁止在**默认** profile 上开调试端口,所以必须用**拷贝出来的独立目录**(`~/.feishu/auto_udd`)。

## 数据与接口

| 数据 | 来源接口 | 落库表 |
|---|---|---|
| 额度(总额度/已用/剩余) | `ai_center/overview/feature` + `homepage/ai_product_info` | `feishu_ai_quota` |
| 趋势(按天按功能点数) | `ai_center/overview/trend` | `feishu_ai_trend` |
| **全员逐人**(姓名/工号/部门/逐功能点数) | `ai_center/usage_detail/entity`(POST,根部门=全员,offset 翻页) | `feishu_ai_member` |

身份:飞书 `externalID` = 飞连 `user_id` → email = `user_id@yourcompany.com`,部门走飞连 `department_path`(缺飞连凭证则用飞书自带姓名/叶子部门兜底)。

## 部署(在你 Mac 上一次)

```bash
bash setup.sh              # 建 venv+chromium、拷 profile、生成 launchd(每天 08:30)
vi ~/.feishu/collector.env # 填 COLLECTOR_URL / COLLECTOR_TOKEN(+ 选填飞连/告警)
bash run_collector.sh      # 手动试跑一次,确认上报 OK
```

服务端(看板 `app.py`)启动时会自动建 `feishu_ai_*` 三表并暴露 `POST /v1/feishu/report`(Bearer 鉴权)。

## 日常运维(几乎零维护)

- 每天 08:30 launchd 自动跑;这次访问的心跳让登录态滚动续期 → 长期不用重登。
- **登录态真失效时**(采集器退出码 3 + 飞书告警):在日常 Chrome 里打开 `your-tenant.feishu.cn/admin` 确认还登着 → `bash refresh_profile.sh` 刷新拷贝即可。几周一次,告警驱动。
- 日志:`~/.feishu/collector.log`

## 手动/调试

```bash
# 只抓不报,打印归一化结果
FEISHU_DRY_RUN=1 FEISHU_CDP=http://127.0.0.1:9223 ../../.venv-feishu/bin/python feishu_collector.py
```

## 文件

| 文件 | 作用 |
|---|---|
| `feishu_collector.py` | 采集器:CDP 抓 + 归一化 + 上报 |
| `feishu_login.py` | (备用)独立 Playwright 登录存 storageState |
| `refresh_profile.sh` | 拷贝/刷新登录 profile → `~/.feishu/auto_udd` |
| `run_collector.sh` | 每日主流程:确保 Chrome+端口 → 跑采集 → 失效告警 |
| `setup.sh` | 一键部署 |
| `schema_feishu.sql` | 独立三表(由 `app.py` 启动加载) |
| `collector.env.example` | 配置模板 |
