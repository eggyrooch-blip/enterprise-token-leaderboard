#!/usr/bin/env python3
"""Fail if public files contain private company identifiers.

The guard is intentionally literal and conservative. It is not a secret scanner;
it catches the repo-specific classes of data that make this project unsafe to
publish as a reusable open-source example.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".ftask",
    ".claude",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".omc",
}

SKIP_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
}

TEXT_SUFFIXES = {
    "",
    ".css",
    ".conf",
    ".example",
    ".html",
    ".js",
    ".json",
    ".md",
    ".plist",
    ".py",
    ".service",
    ".sh",
    ".sql",
    ".timer",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

# Org-specific identifiers (employee names + private domains) are NOT hardcoded
# here — that would publish exactly the PII we are trying to keep private. They
# live in a local, git-ignored denylist so each org adds its own. Copy
# scripts/leak-denylist.example.txt -> scripts/leak-denylist.private.txt and fill
# it in (or point LEAK_DENYLIST at any file). Format: one `name: <term>` or
# `domain: <example.com>` per line; `#` comments and blank lines ignored.
def load_denylist() -> tuple[list[str], list[str]]:
    """Return (person_words, org_domains) from the local denylist if present."""
    path = Path(os.environ.get("LEAK_DENYLIST", ROOT / "scripts" / "leak-denylist.private.txt"))
    names: list[str] = []
    domains: list[str] = []
    if not path.exists():
        return names, domains
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if not val:
            continue
        if key.strip().lower() == "name":
            names.append(val)
        elif key.strip().lower() == "domain":
            domains.append(val)
    return names, domains


PERSON_WORDS, ORG_DOMAINS = load_denylist()

PRIVATE_IPV4 = r"(?:10|192\.168|172\.(?:1[6-9]|2\d|3[0-1]))(?:\.\d{1,3}){3}"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("hardcoded collector token", re.compile(r"\btok_[A-Za-z0-9]{16,}\b")),
    ("hardcoded secret key", re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b")),
    ("hardcoded UUID", re.compile(rf"\b{UUID}\b")),
    ("Mac serial-like identifier", re.compile(r"\b(?=[A-Z0-9]{10,12}\b)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]+\b")),
    (
        "private IPv4 address",
        re.compile(rf"(?<![\d.]){PRIVATE_IPV4}(?![\d.])"),
    ),
    (
        "production ssh login",
        re.compile(rf"\b(?:root|it|admin)@{PRIVATE_IPV4}\b"),
    ),
]

_host_alts = list(ORG_DOMAINS) + [r"\.internal", r"\.corp", r"\.lan", r"\.local"]
PATTERNS.append((
    "internal hostname",
    re.compile(r"\b[A-Za-z0-9.-]*(?:" + "|".join(re.escape(d) if d in ORG_DOMAINS else d for d in _host_alts) + r")\b"),
))

if ORG_DOMAINS:
    PATTERNS.append((
        "real employee email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@(?:" + "|".join(re.escape(d) for d in ORG_DOMAINS) + r")\b"),
    ))
if PERSON_WORDS:
    PATTERNS.append((
        "real person identifier",
        re.compile("|".join(re.escape(word) for word in PERSON_WORDS)),
    ))


def iter_repo_files() -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        candidates = [
            ROOT / item.decode("utf-8")
            for item in result.stdout.split(b"\0")
            if item
        ]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        candidates = list(ROOT.rglob("*"))

    files: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        if path.name in SKIP_FILES:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name not in {"Dockerfile"}:
            continue
        files.append(path)
    return files


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_file(path: Path) -> list[str]:
    text = read_text(path)
    if text is None:
        return []

    findings: list[str] = []
    display = path if path.is_absolute() else path
    try:
        display = path.relative_to(ROOT)
    except ValueError:
        pass

    for line_no, line in enumerate(text.splitlines(), 1):
        for label, pattern in PATTERNS:
            for match in pattern.finditer(line):
                if label == "hardcoded secret key" and any(
                    word in match.group(0).lower() for word in ("your", "example", "placeholder")
                ):
                    continue
                findings.append(f"{display}:{line_no}: {label}: {match.group(0)}")
    return findings


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv] if argv else iter_repo_files()
    findings: list[str] = []
    for path in paths:
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    findings.extend(scan_file(child))
        else:
            findings.extend(scan_file(path))

    if findings:
        print("Open-source guard found private identifiers:")
        print("\n".join(findings))
        return 1

    print("Open-source guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
