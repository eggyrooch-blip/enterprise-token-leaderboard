"""email_merge 共享 helper 的回归测试(litellm_collector / cursor_sync 共用)。

惰性读 env + 按值 memoize:采集器常在 import 后才把 .env 灌进 os.environ,
故映射必须惰性解析,否则跨源合并(vendor 分身/外部邮箱→真人 的 Cursor 量)会失效。
(本测试只用 example.com 占位,绝不出现真实员工邮箱/域名。)
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import email_merge as M  # noqa: E402


def test_maps_alias_to_canonical(monkeypatch):
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP",
                       "vendor_v@example.com:vendor@example.com,bob123@gmail.com:bob@example.com")
    assert M.merge_email("vendor_v@example.com") == "vendor@example.com"
    assert M.merge_email("bob123@gmail.com") == "bob@example.com"


def test_case_insensitive_key(monkeypatch):
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP", "Alice_V@example.com:alice@example.com")
    assert M.merge_email("alice_v@example.com") == "alice@example.com"
    assert M.merge_email("ALICE_V@EXAMPLE.COM") == "alice@example.com"


def test_unmapped_and_empty_pass_through(monkeypatch):
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP", "x@example.com:y@example.com")
    assert M.merge_email("someone@example.com") == "someone@example.com"
    assert M.merge_email("") == ""
    assert M.merge_email(None) is None


def test_lazy_reads_env_at_call_time(monkeypatch):
    # import 时 env 可能还没灌;改 env 后下次调用立即生效(memoize 按原始字符串)。
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP", "")
    assert M.merge_email("p_v@example.com") == "p_v@example.com"
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP", "p_v@example.com:p@example.com")
    assert M.merge_email("p_v@example.com") == "p@example.com"


def test_no_env_returns_input(monkeypatch):
    monkeypatch.delenv("LITELLM_EMAIL_MERGE_MAP", raising=False)
    assert M.merge_email("anyone@example.com") == "anyone@example.com"
