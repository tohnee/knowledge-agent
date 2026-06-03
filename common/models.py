"""WKA-Scale shared models — used by all three pillars.
Plain dataclasses so the code runs with zero external deps for the test harness;
in production these map to Pydantic + DB rows."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import hashlib, time


class Tier(str, Enum):
    """Extraction tier — decides how much LLM budget a doc gets (Pillar 1)."""
    A = "A"   # 满血编译：大模型整章抽取 + Wiki 页 + 交叉链接
    B = "B"   # 轻编译：小模型抽实体+摘要
    C = "C"   # 懒编译：仅向量化，命中后再升级


class SourceTier(str, Enum):
    OFFICIAL = "official"
    ANALYST = "analyst"
    RUMOR = "rumor"


class DocStatus(str, Enum):
    INGESTING = "ingesting"
    PARSED = "parsed"
    EXTRACTED = "extracted"
    LINKED = "linked"
    FAILED = "failed"


@dataclass
class Document:
    id: str
    name: str
    text: str                       # MinerU structured markdown (whole-doc)
    source_tier: SourceTier = SourceTier.ANALYST
    tier: Optional[Tier] = None     # extraction tier (set by classifier)
    controlled: bool = False
    status: DocStatus = DocStatus.INGESTING
    sha256: str = ""
    meta: dict = field(default_factory=dict)

    def fingerprint(self) -> str:
        self.sha256 = hashlib.sha256(self.text.encode()).hexdigest()
        return self.sha256


@dataclass
class Chunk:
    """Child chunk — fine-grained, embedded for precise recall (Pillar 1 §1.2)."""
    id: str
    doc_id: str
    text: str
    parent_id: str                  # → Wiki page / section span (parent return)
    section: str = ""
    embedding: Optional[list] = None
    meta: dict = field(default_factory=dict)


@dataclass
class WikiPage:
    """Parent span — full context returned to the LLM (avoids fragmented chunks)."""
    id: str
    doc_id: str
    title: str
    summary: str
    body: str
    entities: list = field(default_factory=list)
    section: str = ""


@dataclass
class Entity:
    id: str
    name: str
    type: str
    aliases: list = field(default_factory=list)
    embedding: Optional[list] = None
    doc_ids: list = field(default_factory=list)


@dataclass
class Relation:
    src: str
    dst: str
    lt: str                         # link type
    doc_id: str = ""


@dataclass
class RetrievalCandidate:
    """Flows through the retrieval funnel (Pillar 2)."""
    chunk_id: str
    text: str
    parent_id: str
    dense_score: float = 0.0
    sparse_score: float = 0.0
    rrf_score: float = 0.0
    rerank_score: float = 0.0
    confidence: float = 0.0
    meta: dict = field(default_factory=dict)


def now_ms() -> int:
    return int(time.time() * 1000)
