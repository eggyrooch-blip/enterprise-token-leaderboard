import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "collector" / "dashboard.html"


def test_cursor_leaderboard_request_uses_global_date_range():
    html = DASHBOARD.read_text(encoding="utf-8")
    # cursor 现走懒加载 TAB_EP(2026-06-23 性能优化:首屏/改区间不再一次拉 13 个接口),
    # 请求由 ensureTab 用 curRangeQS() 拼上所选区间(from/to)。要求未变:cursor 跟随全局区间。
    assert "ep:'/v1/cursor'" in html, "TAB_EP 必须包含 /v1/cursor"
    assert "curRangeQS" in html, "ensureTab 必须用 curRangeQS 拼区间"
    qs = html[html.index("function curRangeQS()"):html.index("function curRangeQS()") + 400]
    assert "from='+RANGE_FROM" in qs and "to='+RANGE_TO" in qs, "区间必须写进请求"


def test_meta_default_range_becomes_request_state():
    """默认页面展示的数据范围必须进入首批 API 请求参数。

    根因(2026-06-18): fillMeta 只把 min/max 写入隐藏 input,没有同步 RANGE_FROM/RANGE_TO。
    结果首屏 UI 显示 2026-02-28→2026-06-18,但 load() 仍按空 RANGE 请求 lifetime;
    用户再点“应用”后才改成 day 桶区间,同一可见区间数字前后不一致。
    """
    html = DASHBOARD.read_text(encoding="utf-8")
    fill_meta = html[html.index("async function fillMeta()"):html.index("var GOVERNANCE_METRICS=")]

    assert "RANGE_FROM=m.min_date" in fill_meta
    assert "RANGE_TO=m.max_date" in fill_meta


def test_dashboard_explains_cost_and_agent_scope():
    html = DASHBOARD.read_text(encoding="utf-8")

    # 去点改造后的新 footer(2026-06-22):两种 $ 口径 + token 一律以纯 token 为准, 「点」独立不并入。
    assert "估算 = 网关实销（litellm/api 计量）+ 订阅席位摊销" in html
    assert "Token 一律以纯 token 为准" in html
    assert "「点」是另一口径，不并入 token 与 cost" in html
    assert "个人榜默认不含归属 Agent 消耗" in html
    assert "/v1/agent_owner_summary" in html
    assert "估算" in html


def test_no_duplicate_outer_range_buttons():
    """近7天/近30天 已并入日历预设, 顶栏不应再有重复的 r7/r30 独立按钮(2026-06-09)。"""
    html = DASHBOARD.read_text(encoding="utf-8")
    assert 'id="r7"' not in html and 'id="r30"' not in html, (
        "顶栏 r7/r30 按钮与日历预设重复, 应删除"
    )
    # 日历内的快捷预设必须仍在(去重不等于砍功能)
    assert 'data-q="d7"' in html and 'data-q="d30"' in html, (
        "日历快捷预设 近7/近30 天必须保留"
    )


def test_hermes_board_is_standalone_after_litellm():
    html = DASHBOARD.read_text(encoding="utf-8")

    litellm_pos = html.index('data-t="litellm"')
    hermes_pos = html.index('data-t="hermes"')
    agent_pos = html.index('data-t="agent"')

    assert litellm_pos < hermes_pos < agent_pos
    assert "Hermes 榜" in html
    # Hermes 榜走懒加载、独立请求 /v1/leaderboard?client=Hermes(2026-06-23:不从 composition 派生 ——
    # Hermes 有大小写变体且 cost 是推断价,单一 composition 公式还原不出,故保留后端独立榜口径)。
    assert "/v1/leaderboard?client=Hermes" in html
    assert "hermes:" in html  # TAB_EP 含 hermes 懒加载条目
    assert "CUR==='hermes'" in html
    assert "'Hermes'" in html[html.index("var TOOL_COLOR="):html.index("function toolColor")]


def test_seg_ctl_capsules_render_adjacent_not_split():
    """根因(2026-06-14): 两个 .seg-ctl 胶囊(总量/日均 与 按Token/按消费)各自带
    margin-left:auto, flex 把空闲空间平分到两个 auto margin → 胶囊间被撑开一大段空隙。
    修复: 基础 .seg-ctl 不再带 margin-left:auto; 只有 #metricCtl 吃 auto 把整组推右,
    两胶囊靠父级 .tabs 的 flex gap 分隔(不另加 margin, 否则和 gap 叠加成 16px)。"""
    html = DASHBOARD.read_text(encoding="utf-8")
    seg_rule = re.search(r"\.seg-ctl\{([^}]*)\}", html)
    assert seg_rule, "应有 .seg-ctl 基础样式"
    assert "margin-left:auto" not in seg_rule.group(1), (
        "基础 .seg-ctl 不得带 margin-left:auto, 否则两个胶囊各自 auto 被撑开"
    )
    assert re.search(r"#metricCtl\{[^}]*margin-left:auto", html), (
        "只有 #metricCtl 吃 margin-left:auto 把整组推到最右"
    )
    assert not re.search(r"#sortCtl\{[^}]*margin-left", html), (
        "#sortCtl 不应另加 margin-left(会和父级 .tabs gap 叠加), 靠 flex gap 分隔即可"
    )
