import os
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "open_source_guard.py"


def run_guard(*args: pathlib.Path, denylist: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if denylist is not None:
        env["LEAK_DENYLIST"] = str(denylist)
    return subprocess.run(
        [sys.executable, str(GUARD), *map(str, args)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=env,
    )


def _write_denylist(tmp_path: pathlib.Path) -> pathlib.Path:
    dl = tmp_path / "denylist.txt"
    dl.write_text(
        "domain: " + "keep" + ".com\n"
        "domain: " + "goto" + "keep" + ".com\n"
        "name: " + "sun" + "ke\n",
        encoding="utf-8",
    )
    return dl


def test_open_source_guard_rejects_private_identifiers(tmp_path):
    leaked = tmp_path / "leaked.md"
    private_email = "sun" + "ke" + "@" + "keep" + ".com"
    private_ip = ".".join(["10", "2", "20", "3"])
    private_host = "litellm.sre." + "goto" + "keep" + ".com"
    private_token = "tok_" + "a" * 24
    private_uuid = "-".join(["f5395438", "f42e", "43b7", "9ca7", "f44f314ecd4e"])
    private_serial = "C02" + "ABCDEFGH"
    leaked.write_text(
        f"contact {private_email} via root@{private_ip} for {private_host}\n"
        f"token {private_token} uuid {private_uuid} serial {private_serial}\n",
        encoding="utf-8",
    )

    result = run_guard(leaked, denylist=_write_denylist(tmp_path))

    assert result.returncode == 1
    assert private_email in result.stdout
    assert private_ip in result.stdout
    assert private_host in result.stdout
    assert private_token in result.stdout
    assert private_uuid in result.stdout
    assert private_serial in result.stdout


def test_open_source_guard_allows_public_example_identifiers(tmp_path):
    clean = tmp_path / "clean.md"
    clean.write_text(
        "contact alex@example.com via collector.example.com for Example Corp\n",
        encoding="utf-8",
    )

    result = run_guard(clean)

    assert result.returncode == 0, result.stdout + result.stderr


def test_open_source_guard_skips_gitignored_env_by_default():
    ignored_env = ROOT / "pipeline" / ".env"
    original = ignored_env.read_text(encoding="utf-8") if ignored_env.exists() else None
    private_token = "tok_" + "b" * 24
    try:
        ignored_env.write_text(f"COLLECTOR_API_TOKENS={private_token}\n", encoding="utf-8")

        default_result = run_guard()
        explicit_result = run_guard(ignored_env)

        assert default_result.returncode == 0, default_result.stdout + default_result.stderr
        assert explicit_result.returncode == 1
        assert private_token in explicit_result.stdout
    finally:
        if original is None:
            ignored_env.unlink(missing_ok=True)
        else:
            ignored_env.write_text(original, encoding="utf-8")


def test_repository_public_files_pass_open_source_guard():
    result = run_guard()

    assert result.returncode == 0, result.stdout + result.stderr
