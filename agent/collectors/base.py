"""采集源插件接口。新增一个 agent 工具 = 写一个 UsageCollector 子类并注册。

约定的归一化 record（这是整个系统的扩展契约，收集端/看板都依赖它）：
    {
      "usage_date": "YYYY-MM-DD",
      "source": "subscription",        # 来源标签，可自定义
      "tool":   "claude_code",         # 工具名
      "model":  "claude-...",
      "input_tokens": int, "output_tokens": int,
      "cache_read_tokens": int, "cache_write_tokens": int,
      "total_tokens": int, "cost_usd": float
    }
"""
from __future__ import annotations

from datetime import date


class UsageCollector:
    name: str = "base"
    source: str = "subscription"

    def __init__(self, conf: dict) -> None:
        self.conf = conf

    def available(self) -> bool:
        """该机器上是否具备采集条件（二进制存在 / 日志目录存在等）。"""
        return True

    def collect(self, day: date) -> list[dict]:
        """返回某一天的归一化 record 列表。子类实现。"""
        raise NotImplementedError


def num(d: dict, *keys: str) -> int:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return 0
