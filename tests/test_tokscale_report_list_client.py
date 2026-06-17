"""回归测试：Windows MDM 上报里 client 字段是 list 时不得把整份上报打成 500。

事故模型：实测某 tokscale 客户端(Windows)把 `models --json` / `graph` entry 的
`client` 字段发成 list,例如 ['claude']。服务端旧逻辑 `_CLIENT_LABELS.get(client_raw)`
直接拿 list 当 dict key →  `TypeError: unhashable type: 'list'` → do_POST 兜底回 500。
结果:这台机器每小时上报一次,每次都 500,数据一条都进不了 tokscale 榜。
本测试锁死修复:list/dict/None 形状的 client 都先归一成可哈希字符串再查表,
上报必须 200 且 client 标签正确落库。
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


class _TokscaleReportHandler:
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
    db_path = tmp_path / "tok.db"
    monkeypatch.setattr(dev_collector, "DB", str(db_path))
    monkeypatch.setattr(
        dev_collector,
        "_resolve_serial",
        lambda serial: {"email": "win-user@example.com", "department": "Eng"},
    )
    handler = _TokscaleReportHandler(payload)
    dev_collector.H._tokscale_report(handler)
    return handler, db_path


def test_list_client_does_not_500_and_normalizes_label(monkeypatch, tmp_path):
    handler, db_path = _run(monkeypatch, tmp_path, {
        "serial": "WIN-SERIAL-LIST",
        "hostname": "WIN-DEVICE-01",
        "os": "Microsoft Windows 11",
        "via": "mdm",
        # 复刻线上崩溃 payload:client 是 list
        "models": {"entries": [
            {"client": ["claude"], "provider": "anthropic", "model": "sonnet",
             "input": 100, "output": 50},
        ]},
        "monthly": {"entries": []},
        "graph": {"contributions": [
            {"date": "2026-06-17", "clients": [
                {"client": ["codex"], "providerId": "openai", "modelId": "gpt",
                 "tokens": {"input": 7, "output": 3}},
            ]},
        ]},
    })

    assert handler.code == 200, "client 为 list 时上报必须成功,不能 500"

    conn = dev_collector.db()
    try:
        lt = conn.execute(
            "SELECT client FROM usage WHERE period_type='lifetime' AND email=?",
            ("win-user@example.com",),
        ).fetchone()
        dy = conn.execute(
            "SELECT client FROM usage WHERE period_type='day' AND email=?",
            ("win-user@example.com",),
        ).fetchone()
    finally:
        conn.close()

    assert lt is not None and lt[0] == "Claude Code", "['claude'] 应归一为 Claude Code"
    assert dy is not None and dy[0] == "Codex CLI", "['codex'] 应归一为 Codex CLI"


def test_weird_client_shapes_are_tolerated(monkeypatch, tmp_path):
    # None / 空 list / dict 形状都不得抛异常
    handler, _ = _run(monkeypatch, tmp_path, {
        "serial": "WIN-SERIAL-WEIRD",
        "via": "mdm",
        "models": {"entries": [
            {"client": None, "model": "m1", "input": 1},
            {"client": [], "model": "m2", "input": 1},
            {"client": {"id": "cursor"}, "model": "m3", "input": 1},
        ]},
        "monthly": {"entries": []},
        "graph": {"contributions": []},
    })
    assert handler.code == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
