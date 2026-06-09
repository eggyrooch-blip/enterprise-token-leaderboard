"""身份解析：决定这台机器的用量算到谁头上。支持「无感知/零输入」上线。

按优先级依次尝试，第一个命中即用：
  1. 环境变量 TOKREPORT_EMAIL（CI / 测试覆盖）
  2. 配置文件 EMPLOYEE_EMAIL（有 MDM/飞连时下发，最稳）
  3. git config user.email（开发者机器几乎都配过 —— 实现零输入自动归属）
  4. 系统登录名 + EMAIL_DOMAIN 拼接（兜底）

这样：有 MDM 的企业靠下发；没有 MDM 的企业靠 git email 自动识别，员工无需任何操作。
"""
from __future__ import annotations

import os
import subprocess


def _git_email() -> str | None:
    try:
        out = subprocess.run(["git", "config", "--global", "user.email"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def resolve(conf: dict) -> tuple[str, str]:
    """返回 (email, dept)。"""
    dept = os.environ.get("TOKREPORT_DEPT") or conf.get("DEPT", "unknown")

    email = os.environ.get("TOKREPORT_EMAIL") or conf.get("EMPLOYEE_EMAIL")
    if email:
        return email, dept

    email = _git_email()
    if email:
        return email, dept

    domain = conf.get("EMAIL_DOMAIN")
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    if domain:
        return f"{user}@{domain}", dept
    return user, dept
