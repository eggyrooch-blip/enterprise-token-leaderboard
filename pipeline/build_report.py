#!/usr/bin/env python3
"""全链路：把"一台开发电脑"的 AI 编程 token 消耗，采集 → 身份解析 → 出报告。

  采集   tokscale models/monthly --json （本地 agent 日志，离线、无感知）
  身份   飞连 device/search 按本机 SN 反查 归属人/部门/活跃状态
  覆盖   飞连 device/search status=1 数全公司活跃 Mac（覆盖率分母）
  产出   dashboard/data/usage.json （中性企业看板直接读）

全程 Alex 少感知：不弹窗、不要求输入；身份由序列号自动解析。
用法：  python3 pipeline/build_report.py
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "collector"))


def load_env(p):
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def tokscale_json(*args):
    out = subprocess.run(["tokscale", *args], capture_output=True, text=True, timeout=180)
    if out.returncode != 0:
        raise RuntimeError(f"tokscale {args} 失败: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def local_serial():
    out = subprocess.run(["system_profiler", "SPHardwareDataType"],
                         capture_output=True, text=True, timeout=30).stdout
    for line in out.splitlines():
        if "Serial Number" in line:
            return line.split(":", 1)[1].strip()
    return None


def git_email():
    try:
        return subprocess.run(["git", "config", "--global", "user.email"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


# 工具展示名
TOOL_LABEL = {
    "claude": "Claude Code", "codex": "Codex CLI", "gemini": "Gemini CLI",
    "cursor": "Cursor", "opencode": "OpenCode", "kimi": "Kimi CLI",
    "amp": "Amp", "droid": "Droid", "openclaw": "OpenClaw", "pi": "Pi",
}


def collect_usage():
    """tokscale models --json → 按工具/按模型聚合；monthly --json → 月度趋势。"""
    models = tokscale_json("models", "--json").get("entries", [])
    by_tool, by_model = {}, []
    tot = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
           "tokens": 0, "messages": 0, "cost": 0.0}
    for e in models:
        toks = (e.get("input", 0) + e.get("output", 0) + e.get("cacheRead", 0)
                + e.get("cacheWrite", 0) + e.get("reasoning", 0))
        cost = e.get("cost", 0.0)
        msgs = e.get("messageCount", 0)
        client = e.get("client", "unknown")
        t = by_tool.setdefault(client, {"tool": client, "label": TOOL_LABEL.get(client, client),
                                        "tokens": 0, "cost": 0.0, "messages": 0, "models": 0})
        t["tokens"] += toks; t["cost"] += cost; t["messages"] += msgs; t["models"] += 1
        by_model.append({"tool": client, "label": TOOL_LABEL.get(client, client),
                         "model": e.get("model"), "tokens": toks, "cost": round(cost, 2),
                         "messages": msgs})
        tot["input"] += e.get("input", 0); tot["output"] += e.get("output", 0)
        tot["cacheRead"] += e.get("cacheRead", 0); tot["cacheWrite"] += e.get("cacheWrite", 0)
        tot["tokens"] += toks; tot["messages"] += msgs; tot["cost"] += cost

    monthly = tokscale_json("monthly", "--json").get("entries", [])
    months = []
    for m in monthly:
        mt = m.get("input", 0) + m.get("output", 0) + m.get("cacheRead", 0) + m.get("cacheWrite", 0)
        if not mt and "models" in m:  # 某些版本月度只给模型名，回退用 total 字段
            mt = m.get("total", 0) or m.get("tokens", 0)
        months.append({"month": m.get("month"), "tokens": mt, "cost": round(m.get("cost", 0.0), 2)})

    tools = sorted(by_tool.values(), key=lambda x: x["tokens"], reverse=True)
    for t in tools:
        t["cost"] = round(t["cost"], 2)
    by_model.sort(key=lambda x: x["tokens"], reverse=True)
    tot["cost"] = round(tot["cost"], 2)
    return {"totals": tot, "by_tool": tools, "by_model": by_model[:12],
            "by_month": months, "active_days_est": len([m for m in months if m["tokens"] > 0])}


def resolve_identity():
    """飞连：本机 SN → 归属人/部门/活跃状态；邮箱二次确认。失败则降级到本地。"""
    serial = local_serial()
    ident = {"serial": serial, "source": "local", "name": None, "email": git_email(),
             "department": None, "is_active_terminal": None, "device_name": None,
             "model": None, "login_user": os.environ.get("USER")}
    try:
        from feilian_client import FeilianClient
        fc = FeilianClient()
        dev = fc.device_by_serial(serial) if serial else None
        if dev:
            ident.update({
                "source": "feilian",
                "name": dev.get("full_name"),
                "department": dev.get("department_name"),
                "is_active_terminal": bool(dev.get("is_live")) and dev.get("device_status") == 1,
                "device_name": dev.get("device_name"),
                "model": dev.get("model"),
                "did": dev.get("did"),
                "user_id": dev.get("user_id"),
            })
            li = (dev.get("device_info") or {}).get("login_user")
            if li:
                ident["login_user"] = li
        # 邮箱补全（device 不带 email，用成员搜索补）
        if ident["email"]:
            try:
                root = fc.root_department_id()
                u = fc.user_by_email(ident["email"], root)
                if u:
                    ident["email"] = u.get("email") or ident["email"]
                    ident["department"] = ident["department"] or u.get("department_path")
                    ident["name"] = ident["name"] or u.get("full_name")
                    ident["people_status"] = u.get("people_status")
            except Exception:
                pass
        ident["fleet"] = {"active_total": fc.active_device_count(),
                          "active_mac": fc.active_device_count(client_os="mac")}
    except Exception as e:
        ident["feilian_error"] = str(e)
    return ident


def main():
    load_env(ROOT / "pipeline" / ".env")
    print("[1/3] 采集本机 token 用量（tokscale）…", file=sys.stderr)
    usage = collect_usage()
    print("[2/3] 飞连解析身份 + 覆盖率…", file=sys.stderr)
    ident = resolve_identity()
    report = {
        "generated_hint": "run pipeline/build_report.py to refresh",
        "person": ident,
        "usage": usage,
    }
    out = ROOT / "dashboard" / "data" / "usage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(report, ensure_ascii=False, indent=2)
    out.write_text(blob)
    # file:// 友好：看板用 <script src> 读全局，避免本地 fetch 的 CORS 限制
    (out.parent / "usage.js").write_text("window.__REPORT__ = " + blob + ";\n")
    print(f"[3/3] 已写 {out} + usage.js", file=sys.stderr)
    # 控制台速览
    t = usage["totals"]
    print(f"\n  归属: {ident.get('name')} · {ident.get('department')}", file=sys.stderr)
    print(f"  活跃终端: {ident.get('is_active_terminal')}  机型: {ident.get('model')}", file=sys.stderr)
    print(f"  总 tokens: {t['tokens']:,}  成本: ${t['cost']:,.2f}  消息: {t['messages']:,}", file=sys.stderr)
    fleet = ident.get("fleet", {})
    print(f"  覆盖率分母: 活跃 Mac {fleet.get('active_mac')} / 全部 {fleet.get('active_total')}", file=sys.stderr)


if __name__ == "__main__":
    main()
