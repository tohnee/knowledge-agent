"""Pillar 2 · Stage 0 — Query Router.
Decides retrieval strategy BEFORE touching the index:
  · local  → entity-centric factual question → vector+BM25+graph-neighbor
  · global → corpus-wide thematic question → community summaries (map-reduce)
Also builds metadata pre-filters (department/time/clearance) to shrink the candidate domain."""
from __future__ import annotations
import re
from dataclasses import dataclass, field


GLOBAL_SIGNALS = [
    "趋势", "总体", "整体", "主要", "概览", "全部", "所有", "汇总", "格局", "主题",
    "trend", "overall", "summary", "across", "main themes", "landscape", "in general",
]
# strong local signals: specific entity patterns / "how much / what is X"
LOCAL_SIGNALS = ["多少", "是什么", "什么状态", "具体", "how much", "what is", "status of"]


@dataclass
class QueryPlan:
    query: str
    mode: str = "local"             # local | global
    shard_keys: list = field(default_factory=list)   # department partitions to search
    filters: dict = field(default_factory=dict)      # metadata pre-filter
    top_recall: int = 30
    top_rerank: int = 8
    conf_threshold: float = 0.5


def route(query: str, role: str = "analyst",
          department: str | None = None) -> QueryPlan:
    plan = QueryPlan(query=query)

    q = query.lower()
    global_hits = sum(1 for s in GLOBAL_SIGNALS if s in q)
    local_hits  = sum(1 for s in LOCAL_SIGNALS if s in q)
    has_specific_entity = bool(re.search(r"\bN[0-9]+|\bHBM|\bEUV|JESD[0-9]+", query))

    if global_hits > local_hits and not has_specific_entity:
        plan.mode = "global"
    else:
        plan.mode = "local"

    # metadata pre-filter: clearance (Dynamic Security) + department partition
    if role == "viewer":
        plan.filters["controlled"] = False        # viewers never see controlled
    if department:
        plan.shard_keys = [department]

    return plan
