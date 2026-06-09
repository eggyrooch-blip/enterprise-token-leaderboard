import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "collector" / "dashboard.html"
HELP = ROOT / "collector" / "help.html"
BIG_TECH = ROOT / "BIG-TECH-PATTERNS.md"
ARCHITECTURE = ROOT / "ARCHITECTURE.md"


REQUIRED_METRIC_IDS = [
    "cost_efficiency",
    "adoption_coverage",
    "code_acceptance",
    "delivery_quality",
    "reliability_budget",
    "privacy_purpose",
    "collection_health",
]


def test_dashboard_renders_big_tech_governance_metrics():
    html = DASHBOARD.read_text(encoding="utf-8")

    assert 'data-t="governance"' in html
    assert "/assets/company-logo.svg" in html
    assert "keep-logo.svg" not in html
    assert "大厂治理指标" in html
    assert "GOVERNANCE_METRICS" in html
    assert "/v1/governance_metrics" in html
    assert "CACHE.governance" in html
    assert "renderGovernance" in html

    for metric_id in REQUIRED_METRIC_IDS:
        assert metric_id in html


def test_public_pages_use_generic_company_logo_path():
    text = "\n".join([
        DASHBOARD.read_text(encoding="utf-8"),
        HELP.read_text(encoding="utf-8"),
    ])

    assert "/assets/company-logo.svg" in text
    assert "keep-logo.svg" not in text
    assert "KeepSans" not in text
    assert ("tokscale." + "goto" + "keep" + ".com") not in text


def test_big_tech_docs_map_sources_to_metrics():
    text = "\n".join(
        [
            BIG_TECH.read_text(encoding="utf-8"),
            ARCHITECTURE.read_text(encoding="utf-8"),
        ]
    )

    for phrase in [
        "Meta",
        "Policy Zones",
        "Google/DORA",
        "change lead time",
        "deployment frequency",
        "error budget",
        "Tesla",
        "Data Sharing",
        "purpose",
    ]:
        assert phrase in text

    for metric_id in REQUIRED_METRIC_IDS:
        assert metric_id in text
