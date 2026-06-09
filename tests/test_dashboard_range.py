import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "collector" / "dashboard.html"


def test_cursor_leaderboard_request_uses_global_date_range():
    html = DASHBOARD.read_text(encoding="utf-8")
    match = re.search(r"j\('/v1/cursor'(?P<suffix>[^)]*)\)", html)

    assert match, "dashboard must request /v1/cursor"
    assert "+q" in match.group("suffix"), (
        "Cursor leaderboard must use the same ?from=...&to=... range as other rankings"
    )


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
