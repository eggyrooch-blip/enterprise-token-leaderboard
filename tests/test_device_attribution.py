"""回归测试：转手机器的用量必须归到「当前使用者」，而非前任。

事故模型：同一序列号的 Mac 从旧主人转给新主人。飞连保留新旧两条设备记录：
  - Old Owner  device_status=0(退还) is_live=False  updated_time 较旧
  - Alex Chen  device_status=1(在用) is_live=True   updated_time 较新
旧逻辑 device_by_serial 取 devices[0] → 命中旧主人 → 新主人的 Claude 用量
全归到了旧部门。本测试锁死修复后的选择规则。
"""
import os
import sys
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

from feilian_client import FeilianClient  # noqa: E402


def _client():
    return FeilianClient(endpoint="http://x", access_key_id="k", access_key_secret="s")


# 同一序列号的两条记录：旧主人(退还) + 新主人(在用)。顺序故意把旧记录放前面，
# 模拟 API 把 devices[0] 给成退还记录的真实情况。
_TRANSFERRED = {
    "devices": [
        {"full_name": "Old Owner", "department_name": "Example Corp/Strategy",
         "user_id": "ou_old", "device_status": 0, "is_live": False,
         "updated_time": 1775625317},
        {"full_name": "Alex Chen", "department_name": "Example Corp/AI Platform/Sports Science",
         "user_id": "ou_new", "device_status": 1, "is_live": True,
         "updated_time": 1780923840},
    ]
}


def test_device_by_serial_picks_active_owner_not_first(monkeypatch):
    fc = _client()
    monkeypatch.setattr(fc, "_request", lambda *a, **k: _TRANSFERRED)
    dev = fc.device_by_serial("DEMO-SN-001")
    assert dev["full_name"] == "Alex Chen", "转手机器必须归到在用者(device_status=1)，不是 devices[0]"
    assert dev["department_name"].endswith("Sports Science")


def test_device_by_serial_prefers_newest_when_status_tied(monkeypatch):
    fc = _client()
    data = {"devices": [
        {"full_name": "A", "device_status": 1, "is_live": False, "updated_time": 100},
        {"full_name": "B", "device_status": 1, "is_live": False, "updated_time": 200},
    ]}
    monkeypatch.setattr(fc, "_request", lambda *a, **k: data)
    assert fc.device_by_serial("X")["full_name"] == "B"


def test_device_by_serial_none_when_empty(monkeypatch):
    fc = _client()
    monkeypatch.setattr(fc, "_request", lambda *a, **k: {"devices": []})
    assert fc.device_by_serial("nope") is None


# ---- 服务端身份反解：按 open_id 精确命中，防同名串号 ----

class _FakeFC:
    """最小飞连替身：device_by_serial 返回在用者；user/list 返回两个同名不同人。"""
    def device_by_serial(self, serial):
        return {"full_name": "Alex Chen", "department_name": "Example Corp/Strategy",
                "user_id": "ou_new", "icon_url": ""}

    def root_department_id(self):
        return "root"

    def _request(self, method, path, query=None, body=None, auth=True):
        # 两个同名员工：错的(无邮箱)排在前，对的(open_id=ou_new)在后
        return {"user_list": [
            {"full_name": "Alex Chen", "id": "ou_wrong", "email": "",
             "department_path": "Example Corp/Contractors"},
            {"full_name": "Alex Chen", "id": "ou_new", "email": "alex.chen@example.com",
             "department_path": "Example Corp/AI Platform/Sports Science"},
        ]}


def test_resolve_serial_matches_email_by_open_id(monkeypatch):
    import dev_collector
    monkeypatch.setattr(dev_collector, "_fc", _FakeFC())
    dev_collector._serial_cache.clear()
    out = dev_collector._resolve_serial("DEMO-SN-001")
    assert out["email"] == "alex.chen@example.com", "必须按 open_id 命中正确的员工，而非第一个同名"
    # 命中用户档案后，部门以用户路径为准（覆盖设备记录里的旧部门）
    assert out["department"].endswith("Sports Science")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
