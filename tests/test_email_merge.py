"""email_merge 共享 helper 的回归测试(litellm_collector / cursor_sync 共用)。

惰性读 env + 按值 memoize:采集器常在 import 后才把 .env 灌进 os.environ,
故映射必须惰性解析,否则跨源合并(caoxiong_v→曹雄 的 Cursor 量)会失效。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))

import email_merge as M  # noqa: E402


def test_maps_alias_to_canonical(monkeypatch):
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP",
                       "caoxiong_v@keep.com:caoxiong@keep.com,bob123@gmail.com:bob@keep.com")
    assert M.merge_email("caoxiong_v@keep.com") == "caoxiong@keep.com"
    assert M.merge_email("bob123@gmail.com") == "bob@keep.com"


def test_case_insensitive_key(monkeypatch):
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP", "Alice_V@Keep.com:alice@keep.com")
    assert M.merge_email("alice_v@keep.com") == "alice@keep.com"
    assert M.merge_email("ALICE_V@KEEP.COM") == "alice@keep.com"


def test_unmapped_and_empty_pass_through(monkeypatch):
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP", "x@keep.com:y@keep.com")
    assert M.merge_email("someone@keep.com") == "someone@keep.com"
    assert M.merge_email("") == ""
    assert M.merge_email(None) is None


def test_lazy_reads_env_at_call_time(monkeypatch):
    # import 时 env 可能还没灌;改 env 后下次调用立即生效(memoize 按原始字符串)。
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP", "")
    assert M.merge_email("p_v@keep.com") == "p_v@keep.com"
    monkeypatch.setenv("LITELLM_EMAIL_MERGE_MAP", "p_v@keep.com:p@keep.com")
    assert M.merge_email("p_v@keep.com") == "p@keep.com"


def test_no_env_returns_input(monkeypatch):
    monkeypatch.delenv("LITELLM_EMAIL_MERGE_MAP", raising=False)
    assert M.merge_email("anyone@keep.com") == "anyone@keep.com"
