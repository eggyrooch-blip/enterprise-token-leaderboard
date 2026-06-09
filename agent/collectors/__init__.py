"""采集源注册表。加新工具：实现 UsageCollector 子类，在这里登记一行即可。"""
from __future__ import annotations

from .base import UsageCollector
from .claude_code_collector import ClaudeCodeCollector
from .tokscale_collector import TokscaleCollector

REGISTRY: dict[str, type[UsageCollector]] = {
    TokscaleCollector.name: TokscaleCollector,        # 一把覆盖 25+ 工具
    ClaudeCodeCollector.name: ClaudeCodeCollector,    # 零依赖，仅 Claude Code（参考实现）
}


def build(names: list[str], conf: dict) -> list[UsageCollector]:
    """按配置里的 COLLECTORS 顺序实例化，跳过本机不具备条件的。"""
    out = []
    for n in names:
        cls = REGISTRY.get(n.strip())
        if cls is None:
            continue
        inst = cls(conf)
        if inst.available():
            out.append(inst)
    return out
