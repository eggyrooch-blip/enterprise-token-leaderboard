"""默认采集源：调 tokscale 一把覆盖 25+ 工具(Claude Code/Codex/Cursor/Gemini...)。

适合：愿意分发一个 tokscale 二进制、想要最大工具覆盖面的企业。
不同 tokscale 版本 JSON 字段名可能不同，归一化集中在 _normalize() 一处。
"""
from __future__ import annotations

import json
import subprocess
from datetime import date

from .base import UsageCollector, num


class TokscaleCollector(UsageCollector):
    name = "tokscale"
    source = "subscription"

    def __init__(self, conf: dict) -> None:
        super().__init__(conf)
        self.binary = conf.get("TOKSCALE_BIN", "/usr/local/bin/tokscale")

    def available(self) -> bool:
        import os
        return os.path.exists(self.binary)

    def collect(self, day: date) -> list[dict]:
        out = subprocess.run(
            [self.binary, "--json", "--since", day.isoformat(), "--until", day.isoformat()],
            capture_output=True, text=True, check=True,
        ).stdout
        return self._normalize(json.loads(out), day)

    def _normalize(self, payload, day: date) -> list[dict]:
        if isinstance(payload, dict):
            rows = payload.get("records") or payload.get("clients") or payload.get("data") or []
        elif isinstance(payload, list):
            rows = payload
        else:
            rows = []

        records = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            rec = {
                "usage_date": day.isoformat(),
                "source": self.source,
                "tool": str(r.get("client") or r.get("tool") or r.get("agent") or "unknown"),
                "model": str(r.get("model") or "unknown"),
                "input_tokens": num(r, "input_tokens", "inputTokens", "input"),
                "output_tokens": num(r, "output_tokens", "outputTokens", "output"),
                "cache_read_tokens": num(r, "cache_read_tokens", "cacheReadTokens", "cache_read"),
                "cache_write_tokens": num(r, "cache_write_tokens", "cache_creation_tokens",
                                          "cacheCreationTokens", "cache_write"),
                "total_tokens": num(r, "total_tokens", "totalTokens", "tokens"),
                "cost_usd": float(r.get("cost") or r.get("cost_usd") or 0.0),
            }
            if any(rec[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                    "cache_write_tokens", "total_tokens")):
                records.append(rec)
        return records
