"""litellm key → 归属人 的回归测试。

根因(2026-06-09 排障): /user/daily/activity 里残留的 tmp-* 探针 key 和无别名孤儿 key
(均为 master key 创建、无真人 owner、用完即删)会被合成成 litellm-key:<alias> 假身份,
污染个人榜并掉进 Keep/未归类。修复: build_rows 过滤这两类噪音, 并为真实管理员代建 key
(有 created_by)与运营手工映射(KEY_OWNER_MAP)补归属。这些断言在修复前会失败。
"""
import importlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))


@pytest.fixture()
def L(monkeypatch):
    monkeypatch.setenv("LITELLM_PROBE_ALIAS_PREFIXES", "tmp-")
    monkeypatch.setenv("LITELLM_KEY_OWNER_MAP", "legacy-admin:ops@example.com")
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP",
                       "alice_v@example.com:alice@example.com,bob123@gmail.com:bob@example.com")
    import litellm_collector
    return importlib.reload(litellm_collector)


def _activity(alias):
    """构造一天、一模型、一 key 的 activity 明细。"""
    tok = "tok-" + alias
    return {"date": "2026-06-09", "breakdown": {"models": {"gpt-4o": {"api_key_breakdown": {
        tok: {"metrics": {"prompt_tokens": 100, "completion_tokens": 50,
                          "successful_requests": 3, "spend": 0.01},
              "metadata": {"key_alias": alias, "team_id": None}}}}}}}


def _orphan_noalias():
    """无别名孤儿: metadata.key_alias=None, 已从 /key/list 删除 → alias 退化成 token 前缀。"""
    tok = "deadbeef" + "0" * 56  # tok[:8] == 'deadbeef'
    return {"date": "2026-06-09", "breakdown": {"models": {"gpt-4o": {"api_key_breakdown": {
        tok: {"metrics": {"prompt_tokens": 10, "completion_tokens": 5,
                          "successful_requests": 1, "spend": 0.001},
              "metadata": {"key_alias": None, "team_id": None}}}}}}}


def _lifetime_emails(rows):
    return {r[0] for r in rows if r[2] == "lifetime"}


def test_tmp_probe_keys_are_filtered_out(L):
    results = [_activity("tmp-finalprobe"), _activity("tmp-probe2-20260609")]
    rows, names, agent_owner, stats = L.build_rows(results, {}, {}, "", {})
    assert _lifetime_emails(rows) == set(), "tmp-* 探针 key 不该出现在任何榜"
    assert stats["probe_skipped"] == 2


def test_aliasless_orphan_key_is_filtered_out(L):
    rows, names, agent_owner, stats = L.build_rows([_orphan_noalias()], {}, {}, "", {})
    assert _lifetime_emails(rows) == set(), "无别名无主孤儿 key 不该进榜"
    assert stats["probe_skipped"] == 1


def test_tmp_prefix_filtered_even_with_real_owner(L):
    # 策略固化(应 Codex review): tmp-* 是无条件剔除, 哪怕 key 绑了真人 owner —— 探针就是探针。
    users = {"u-li": {"email": "li@example.com", "name": "李四"}}
    key_map = {"tok-tmp-li": {"user_id": "u-li", "team_id": None, "key_alias": "tmp-li",
                              "user_email": None, "created_by": None, "expires": "2027-01-01"}}
    rows, _n, _a, stats = L.build_rows([_activity("tmp-li")], key_map, users, "", {})
    assert _lifetime_emails(rows) == set(), "tmp-* 无条件剔除, 即便绑了真人 owner"
    assert stats["probe_skipped"] == 1


def test_fetch_daily_activity_reads_all_pages(L, monkeypatch):
    """回归(应 Codex review): 该端点每页只回少量条目, 必须翻到 total_pages, 不能 len<size 提前停。"""
    pages = {
        1: {"results": [{"date": "2026-06-09"}], "metadata": {"total_pages": 3}},
        2: {"results": [{"date": "2026-06-08"}], "metadata": {"total_pages": 3}},
        3: {"results": [{"date": "2026-06-07"}], "metadata": {"total_pages": 3}},
    }
    seen = []

    def fake_get(path, params=None):
        seen.append(params["page"])
        return pages[params["page"]]

    monkeypatch.setattr(L, "_get", fake_get)
    res = L.fetch_daily_activity()
    assert seen == [1, 2, 3], "必须翻完全部 3 页(旧 bug 会在第 1 页就停)"
    assert {e["date"] for e in res} == {"2026-06-07", "2026-06-08", "2026-06-09"}


def test_fetch_daily_activity_stops_on_empty_page_without_total_pages(L, monkeypatch):
    """无 total_pages 时靠空页判停, 不会死循环/拉到 200 页上限。"""
    pages = {
        1: {"results": [{"date": "2026-06-09"}], "metadata": {}},
        2: {"results": [], "metadata": {}},
    }
    seen = []

    def fake_get(path, params=None):
        seen.append(params["page"])
        return pages.get(params["page"], {"results": [], "metadata": {}})

    monkeypatch.setattr(L, "_get", fake_get)
    res = L.fetch_daily_activity()
    assert seen == [1, 2], "空页即停, 不应继续翻"
    assert len(res) == 1


def test_created_by_resolves_owner_when_key_has_no_user(L):
    # key 无 user_id, 但 /key/list 记录了 created_by → 用创建者归属
    users = {"u-admin": {"email": "creator@example.com", "name": "管理员"}}
    key_map = {"tok-代建key": {"user_id": None, "team_id": None, "key_alias": "代建key",
                              "user_email": None, "created_by": "u-admin",
                              "expires": "2027-01-01"}}
    rows, *_ = L.build_rows([_activity("代建key")], key_map, users, "", {})
    assert "creator@example.com" in _lifetime_emails(rows)


def test_owner_override_map_pins_email(L):
    # 既无 user_id 也无 created_by, 但运营在 KEY_OWNER_MAP 钉了 legacy-admin → ops@example.com
    key_map = {"tok-legacy-admin": {"user_id": None, "team_id": None, "key_alias": "legacy-admin",
                                    "user_email": None, "created_by": None,
                                    "expires": "2027-01-01"}}
    rows, *_ = L.build_rows([_activity("legacy-admin")], key_map, {}, "", {})
    assert "ops@example.com" in _lifetime_emails(rows)


def test_real_aliased_key_without_owner_is_kept_not_dropped(L):
    # 有真实别名(非 token 前缀)但暂无 owner 的已删 key: 仍保留为合成身份, 不当噪音误删。
    rows, *_ = L.build_rows([_activity("zhang-coding")], {}, {}, "", {})
    assert "litellm-key:zhang-coding" in _lifetime_emails(rows)


def test_email_merge_rolls_vendor_alias_into_real_person(L):
    # vendor 分身邮箱 alice_v@ → 规范 alice@, 并取规范用户的中文名(不带分身旧名)
    users = {"u-alice": {"email": "alice@example.com", "name": "爱丽丝"}}
    key_map = {"tok-vkey": {"user_id": None, "team_id": None, "key_alias": "vkey",
                            "user_email": "alice_v@example.com", "created_by": None,
                            "expires": "2027-01-01"}}
    rows, names, *_ = L.build_rows([_activity("vkey")], key_map, users, "", {})
    emails = _lifetime_emails(rows)
    assert "alice@example.com" in emails
    assert "alice_v@example.com" not in emails
    assert dict(names)["alice@example.com"] == ("爱丽丝", False)


def test_email_merge_handles_external_gmail(L):
    # 外部 gmail 注册的 litellm 用户 → 归并到员工邮箱
    key_map = {"tok-g": {"user_id": None, "team_id": None, "key_alias": "g",
                         "user_email": "bob123@gmail.com", "created_by": None,
                         "expires": "2027-01-01"}}
    rows, *_ = L.build_rows([_activity("g")], key_map, {}, "", {})
    emails = _lifetime_emails(rows)
    assert "bob@example.com" in emails and "bob123@gmail.com" not in emails
