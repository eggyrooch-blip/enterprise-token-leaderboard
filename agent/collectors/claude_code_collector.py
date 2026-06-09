"""零依赖采集源：直接解析 Claude Code 本地 JSONL（~/.claude/projects/**/*.jsonl）。

适合：不想分发任何额外二进制、只需覆盖 Claude Code 的企业。也是「如何自己写一个
采集源」的参考实现——照此再加 codex_collector / gemini_collector 即可。

只读 token 计数，不读取/不上传任何 prompt 或代码内容。
按 message id 去重，避免续接会话造成的重复计数。
"""
from __future__ import annotations

import glob
import json
import os
from collections import defaultdict
from datetime import date

from .base import UsageCollector, num


class ClaudeCodeCollector(UsageCollector):
    name = "claude_code"
    source = "subscription"

    def __init__(self, conf: dict) -> None:
        super().__init__(conf)
        root = conf.get("CLAUDE_HOME") or os.path.expanduser("~/.claude")
        self.pattern = os.path.join(root, "projects", "**", "*.jsonl")

    def available(self) -> bool:
        return bool(glob.glob(self.pattern, recursive=True))

    def collect(self, day: date) -> list[dict]:
        target = day.isoformat()
        # (model) -> 累加；按 message id 去重
        agg: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        seen: set[str] = set()

        for path in glob.glob(self.pattern, recursive=True):
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        self._consume(line, target, agg, seen)
            except (OSError, ValueError):
                continue

        records = []
        for model, (pin, pout, cr, cw) in agg.items():
            records.append({
                "usage_date": target,
                "source": self.source,
                "tool": self.name,
                "model": model,
                "input_tokens": pin,
                "output_tokens": pout,
                "cache_read_tokens": cr,
                "cache_write_tokens": cw,
                "total_tokens": pin + pout + cr + cw,
                "cost_usd": 0.0,  # 订阅制无逐次计费；成本留给看板按模型单价折算
            })
        return records

    @staticmethod
    def _consume(line: str, target_day: str, agg, seen) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        ts = ev.get("timestamp", "")
        if not ts.startswith(target_day):
            return
        msg = ev.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            return
        mid = msg.get("id") or ev.get("requestId") or ev.get("uuid")
        if mid:
            if mid in seen:
                return
            seen.add(mid)
        model = msg.get("model") or "unknown"
        a = agg[model]
        a[0] += num(usage, "input_tokens")
        a[1] += num(usage, "output_tokens")
        a[2] += num(usage, "cache_read_input_tokens")
        a[3] += num(usage, "cache_creation_input_tokens")
