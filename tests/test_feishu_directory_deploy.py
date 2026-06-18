import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "deploy.sh"
SERVICE = ROOT / "deploy" / "feishu-directory-sync.service"
TIMER = ROOT / "deploy" / "feishu-directory-sync.timer"
ENV_EXAMPLE = ROOT / "deploy" / ".env.example"
RUNBOOK = ROOT / "deploy" / "RUNBOOK.md"


def test_deploy_wires_feishu_directory_sync_timer():
    script = DEPLOY.read_text(encoding="utf-8")

    assert "collector/feishu_directory_sync.py" in script
    assert "feishu-directory-sync.service" in script
    assert "feishu-directory-sync.timer" in script
    assert "enable --now feishu-directory-sync.timer" in script
    assert "systemctl start feishu-directory-sync.service" in script


def test_feishu_directory_systemd_units_are_oneshot_and_daily():
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")

    assert "Type=oneshot" in service
    assert "EnvironmentFile=__REMOTE_DIR__/.env" in service
    assert "Environment=DEV_DB=__REMOTE_DIR__/tok.db" in service
    assert "feishu_directory_sync.py --db __REMOTE_DIR__/tok.db" in service
    assert "SyslogIdentifier=feishu-directory-sync" in service
    assert "OnCalendar=*-*-* 02:10:00" in timer
    assert "Unit=feishu-directory-sync.service" in timer


def test_ops_docs_include_feishu_directory_sync_prerequisites():
    text = "\n".join([
        ENV_EXAMPLE.read_text(encoding="utf-8"),
        RUNBOOK.read_text(encoding="utf-8"),
    ])

    assert "FEISHU_ROOT_DEPT" in text
    assert "AUTH_ADMIN_EMAILS" in text
    assert "contact-read" in text
    assert "feishu-directory-sync.timer" in text
