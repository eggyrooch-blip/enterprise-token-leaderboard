"""回归测试：serial 字段被 Windows 客户端发成 list(日志行+真SN 混在一起)时,
服务端必须 200,并从中正确取出真序列号 —— 这才是 Windows 机器进不了榜的真根因。

事故模型(复刻 2026-06-17 线上诊断捕获的 payload 形状,标识符已脱敏):Windows
PowerShell 客户端的 Log 函数用 Write-Output,泄漏进 Get-DeviceSerial 返回值,使
serial 变成 ["[tokreport-windows] ... BIOS is <SN>", "... baseboard is <BB>",
"... The meaningful SN should be <SN> ...", "<SN>"]。旧代码
`serial in _serial_cache`(line 80)拿 list 当 dict key → unhashable → 500。
v1/v2 都在 entries 上做防御,没碰这条路径,所以真机照旧 500。本测试锁死:
_clean_serial 从污染 list 里取出末尾真 SN,上报 200 并落 report_log。
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import dev_collector  # noqa: E402


# 复刻线上 serial 形状(标识符脱敏:日志行含空格 + 末尾干净 SN)
_FAKE_SN = "FAKESN01"
_REAL_SERIAL_LIST = [
    "[tokreport-windows] 2026-06-17T17:04:53 The original serial number of BIOS is " + _FAKE_SN,
    "[tokreport-windows] 2026-06-17T17:04:53 The original serial number of baseboard is FAKEBB02",
    "[tokreport-windows] 2026-06-17T17:04:53 The meaningful SN should be " + _FAKE_SN + ", which is directly retrieved from the BIOS serial number.",
    _FAKE_SN,
]


def test_clean_serial_extracts_real_sn():
    assert dev_collector._clean_serial(_REAL_SERIAL_LIST) == _FAKE_SN
    assert dev_collector._clean_serial("PLAINSN9") == "PLAINSN9"       # 正常字符串不变
    assert dev_collector._clean_serial(["  ", "ABC123"]) == "ABC123"
    assert dev_collector._clean_serial([]) == ""
    assert dev_collector._clean_serial(None) == ""


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


def test_real_windows_payload_does_not_500(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_collector, "DB", str(tmp_path / "tok.db"))
    # 序列号反解隔离:本测试只验证 serial=list 不再 500、且解析出真 SN
    captured = {}

    def _fake_resolve(serial):
        captured["serial"] = serial
        return {"email": "win@example.com", "department": "Eng"}

    monkeypatch.setattr(dev_collector, "_resolve_serial", _fake_resolve)

    h = _Handler({
        "serial": _REAL_SERIAL_LIST,
        "email": "",
        "hostname": "WIN-DEVICE-01",
        "os": "Microsoft Windows 11 专业版 10.0.22631",
        "ip": "192.0.2.10",
        "via": "mdm",
        "models": {"entries": []},
        "monthly": {"entries": []},
        "graph": {"contributions": []},
    })
    dev_collector.H._tokscale_report(h)

    assert h.code == 200, "serial 为 list 时必须 200,不能 500"
    assert captured["serial"] == _FAKE_SN, "必须把真 SN 从污染 list 里取出来再反解"

    conn = dev_collector.db()
    try:
        row = conn.execute(
            "SELECT serial,hostname FROM report_log WHERE serial=?", (_FAKE_SN,)).fetchone()
    finally:
        conn.close()
    assert row == (_FAKE_SN, "WIN-DEVICE-01"), row


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
