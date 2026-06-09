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
