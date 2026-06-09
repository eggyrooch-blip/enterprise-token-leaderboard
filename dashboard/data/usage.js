window.__REPORT__ = {
  "generated_hint": "synthetic public demo data; run pipeline/build_report.py for private deployments",
  "person": {
    "serial": "DEMO-SN-001",
    "source": "example_mdm",
    "name": "Alex Chen",
    "email": "alex@example.com",
    "department": "Example Corp/Engineering/Platform",
    "is_active_terminal": true,
    "device_name": "Demo MacBook Pro",
    "model": "MacBookProDemo",
    "login_user": "alex",
    "did": "demo-device-id",
    "user_id": "demo-user-id",
    "people_status": 0,
    "fleet": {
      "active_total": 240,
      "active_mac": 180
    }
  },
  "usage": {
    "totals": {
      "input": 82000000,
      "output": 18500000,
      "cacheRead": 410000000,
      "cacheWrite": 23000000,
      "tokens": 533500000,
      "messages": 18420,
      "cost": 1840.52
    },
    "by_tool": [
      {"tool": "codex", "label": "Codex CLI", "tokens": 278000000, "cost": 950.2, "messages": 9200, "models": 3},
      {"tool": "claude", "label": "Claude Code", "tokens": 188000000, "cost": 710.14, "messages": 6500, "models": 2},
      {"tool": "cursor", "label": "Cursor", "tokens": 67500000, "cost": 180.18, "messages": 2720, "models": 2}
    ],
    "by_model": [
      {"tool": "codex", "label": "Codex CLI", "model": "gpt-coding-demo", "tokens": 178000000, "cost": 650.0, "messages": 5400},
      {"tool": "claude", "label": "Claude Code", "model": "claude-demo", "tokens": 138000000, "cost": 520.12, "messages": 4300},
      {"tool": "cursor", "label": "Cursor", "model": "cursor-demo", "tokens": 67500000, "cost": 180.18, "messages": 2720}
    ],
    "by_month": [
      {"month": "2026-04", "tokens": 126000000, "cost": 410.35},
      {"month": "2026-05", "tokens": 188000000, "cost": 665.72},
      {"month": "2026-06", "tokens": 219500000, "cost": 764.45}
    ]
  }
};
