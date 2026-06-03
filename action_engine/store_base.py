"""WKA business layer · KnowledgeStore base contract.
The Action engine writes ONLY through this interface. Two implementations honor it:
  · InMemoryKnowledgeStore  — zero-dep, for tests / the headless closed loop
  · Neo4jKnowledgeStore     — production graph + bitemporal relations

Both MUST preserve bitemporal append-only semantics:
  - capacity/yield/status observations are APPENDED, never overwritten
  - prior latest observation for the same validTime gets supersededBy set
  - as-of query = latest observation with validTime <= year (decision-time)
  - truth query  = latest observation with supersededBy IS NULL
"""
from __future__ import annotations
from abc import ABC, abstractmethod


class KnowledgeStoreBase(ABC):
    # ── Objects ──
    @abstractmethod
    def get_object(self, oid: str) -> dict | None: ...
    @abstractmethod
    def put_object(self, obj: dict) -> None: ...
    @abstractmethod
    def merge_object(self, existing: dict, incoming: dict) -> None: ...
    @abstractmethod
    def set_controlled(self, oid: str, val: bool, eccn: str = "") -> None: ...
    @abstractmethod
    def bump_confidence(self, oid: str, delta: float) -> None: ...

    # ── Links ──
    @abstractmethod
    def put_link(self, link: dict) -> None: ...

    # ── Wiki pages (parent spans) ──
    @abstractmethod
    def put_wiki(self, doc: dict) -> None: ...
    @abstractmethod
    def get_wiki(self, pid: str) -> dict | None: ...

    # ── Bitemporal observations (append-only) ──
    @abstractmethod
    def append_capacity(self, obj_id: str, wspm: int, valid_time: str,
                        source_tier: str, confidence: float) -> None: ...
    @abstractmethod
    def capacity_asof(self, obj_id: str, year: int) -> dict | None: ...
    @abstractmethod
    def capacity_truth(self, obj_id: str) -> dict | None: ...
    @abstractmethod
    def append_status(self, node_id: str, status: str, event_date: str) -> None: ...

    # ── read helpers (so callers/tests never touch backend internals) ──
    @abstractmethod
    def count_objects_by_title(self, title: str) -> int: ...
