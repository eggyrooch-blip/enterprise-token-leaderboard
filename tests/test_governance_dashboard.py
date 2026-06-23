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


def test_dashboard_embeds_subscription_logo_badges():
    html = DASHBOARD.read_text(encoding="utf-8")

    assert "SUB_LOGOS" in html
    assert ".sub-logo{" in html
    assert ".sub-logo.premium{" in html
    assert "#E5B100" in html

    for fingerprint in [
        "m4.7144 15.9555 4.7174-2.6471",
        "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108",
        "M11.503.131 1.891 5.678a.84.84 0 0 0-.42.726",
        "M23.55 5.067c-1.2038-.002-2.1806.973-2.1806 2.1765",
    ]:
        assert fingerprint in html


def test_dashboard_has_feishu_auth_controls():
    html = DASHBOARD.read_text(encoding="utf-8")

    assert 'id="authbar"' in html
    assert "fetch('/v1/me'" in html
    assert "/v1/auth/login?next=" in html
    assert "/v1/auth/logout" in html
    assert "scope-pill" in html
    assert "me.open_id" in html
    # owned_departments 的明细展示 + me.roles 直读已在「看板页头美化」(commit e5328a7)中按设计移除/
    # 重构为局部变量 —— scope 仍由【后端】owned_departments 强制(dev_collector),前端页头保留
    # 姓名/退出/scope-pill/角色标签即可。此处不再断言已被重构掉的内部 JS 表达式(避免脆性误报)。
    assert "auth-role" in html        # 角色标签仍在(渲染自局部 roles 变量)


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
