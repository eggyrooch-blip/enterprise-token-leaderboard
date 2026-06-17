"""回归测试：tokscale 上报里任意标量字段被发成 list/dict 时,服务端不得 500。

事故模型（2026-06-17 线上实测）：某 tokscale 客户端(Windows)把本该是标量的字段
发成单元素 list —— 不止 client,还有 provider / model / cost / month / date。
v1 只修了 client(`unhashable type: 'list'`),但 provider/model 直接 bind 进
SQLite → InterfaceError,cost 喂 float() → TypeError,统统还是 500,重度机器
(如 PF4WTK1L)部署后仍每次上报失败。本测试锁死「所有标量字段总体强转」修复:
list/dict 形状先剥成标量再入库,上报恒 200 且数值/标签正确落库。
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


class _Handler:
    def __init__(self, payload):
        self.payload = payload

    def _auth(self):
        return True

    def _read_body(self):
        return self.payload

    def _send(self, code, obj):
        self.code = code
        self.response = obj


def _run(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(dev_collector, "DB", str(tmp_path / "tok.db"))
    monkeypatch.setattr(dev_collector, "_resolve_serial",
                        lambda s: {"email": "win@example.com", "department": "Eng"})
    h = _Handler(payload)
    dev_collector.H._tokscale_report(h)
    return h


def test_all_scalar_fields_as_list_do_not_500(monkeypatch, tmp_path):
    h = _run(monkeypatch, tmp_path, {
        "serial": "WIN-LIST-ALL",
        "via": "mdm",
        "models": {"entries": [
            {"client": ["claude"], "provider": ["anthropic"], "model": ["sonnet"],
             "input": ["100"], "output": [50], "cost": [1.5]},
        ]},
        "monthly": {"entries": [
            {"month": ["2026-06"], "input": [10], "cost": [0.2]},
        ]},
        "graph": {"contributions": [
            {"date": ["2026-06-17"], "clients": [
                {"client": ["codex"], "providerId": ["openai"], "modelId": ["gpt"],
                 "tokens": {"input": [7], "output": [3]}, "cost": [0.1]},
            ]},
        ]},
    })
    assert h.code == 200, "任意标量字段为 list 时上报必须 200,不能 500"

    conn = dev_collector.db()
    try:
        lt = conn.execute(
            "SELECT client,provider,model,input,output,cost FROM usage "
            "WHERE period_type='lifetime' AND email='win@example.com'").fetchone()
        mo = conn.execute(
            "SELECT period,input FROM usage WHERE period_type='month' "
            "AND email='win@example.com'").fetchone()
        dy = conn.execute(
            "SELECT period,client,provider,model,input FROM usage "
            "WHERE period_type='day' AND email='win@example.com'").fetchone()
    finally:
        conn.close()

    # list 被剥成标量,且数值没有被零掉(num 也 unwrap)
    assert lt == ("Claude Code", "anthropic", "sonnet", 100, 50, 1.5), lt
    assert mo == ("2026-06", 10), mo
    assert dy == ("2026-06-17", "Codex CLI", "openai", "gpt", 7), dy


def test_mac_string_shape_unchanged(monkeypatch, tmp_path):
    # Mac 发标量字符串,行为必须与历史完全一致
    h = _run(monkeypatch, tmp_path, {
        "serial": "MAC-OK", "via": "mdm",
        "models": {"entries": [
            {"client": "claude", "provider": "anthropic", "model": "sonnet",
             "input": 10, "cost": 0.3},
        ]},
        "monthly": {"entries": []},
        "graph": {"contributions": []},
    })
    assert h.code == 200
    conn = dev_collector.db()
    try:
        row = conn.execute(
            "SELECT client,provider,model,input,cost FROM usage "
            "WHERE period_type='lifetime' AND email='win@example.com'").fetchone()
    finally:
        conn.close()
    assert row == ("Claude Code", "anthropic", "sonnet", 10, 0.3)


def test_weird_shapes_tolerated(monkeypatch, tmp_path):
    h = _run(monkeypatch, tmp_path, {
        "serial": "WIN-WEIRD", "via": "mdm",
        "models": {"entries": [
            {"client": None, "provider": {}, "model": [], "cost": "oops", "input": "x"},
            {"client": {"id": "cursor"}, "model": [["nested"]], "input": [["5"]]},
        ]},
        "monthly": {"entries": [{"month": [], "input": 1}]},  # 空 month → 跳过
        "graph": {"contributions": [{"date": None, "clients": []}]},
    })
    assert h.code == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
