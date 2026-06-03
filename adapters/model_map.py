"""ADAPTER 1 · Model Mapping
================================================================================
The engine (wka-scale) speaks in dataclasses: Chunk / WikiPage / Entity / Relation /
RetrievalCandidate.  The business layer (wka) persists to Neo4j nodes, Mongo docs and
Qdrant payloads with a different shape.  This module is the ONLY place that knows both.

It also enforces the field contract from the scaling plan §1.2 "multi-level products":
  Chunk     → Qdrant payload (child vector, parent_id points at the returned span)
  WikiPage  → Mongo wka_wiki_pages (parent span)
  Entity    → Neo4j :Object (candidate, must enter via Action)
  Relation  → Neo4j edge (candidate, must enter via Action)

Nothing else in the codebase should translate between the two worlds."""
from __future__ import annotations
from common.models import Chunk, WikiPage, Entity, Relation, RetrievalCandidate


# ─────────────────────────── Chunk ⇄ Qdrant payload ───────────────────────────
def chunk_to_qdrant(c: Chunk) -> dict:
    """Engine Chunk → Qdrant point (id, vector, payload)."""
    return {
        "id": c.id,
        "vector": c.embedding,
        "payload": {
            "chunk_id": c.id, "doc_id": c.doc_id, "parent_id": c.parent_id,
            "text": c.text, "section": c.section,
            "source_tier": c.meta.get("source_tier", "analyst"),
            "controlled": bool(c.meta.get("controlled", False)),
            "doc_name": c.meta.get("doc_name", ""),
            "department": c.meta.get("department", "default"),
        },
    }


def qdrant_to_candidate(point: dict, dense_score: float = 0.0) -> RetrievalCandidate:
    """Qdrant hit → engine RetrievalCandidate (flows through the funnel)."""
    p = point.get("payload", point)
    return RetrievalCandidate(
        chunk_id=p["chunk_id"], text=p.get("text", ""), parent_id=p.get("parent_id", ""),
        dense_score=dense_score, meta=p)


# ─────────────────────────── WikiPage ⇄ Mongo doc ───────────────────────────
def wiki_to_mongo(w: WikiPage, controlled: bool = False) -> dict:
    """Engine WikiPage → Mongo wka_wiki_pages document (the parent span)."""
    return {
        "_id": w.id, "doc_id": w.doc_id, "title": w.title,
        "summary": w.summary, "body": w.body, "section": w.section,
        "entities": w.entities, "controlled": controlled,
        "objectType": "WikiPage", "reviewStatus": "auto", "version": 1,
    }


def mongo_to_wiki(doc: dict) -> WikiPage:
    return WikiPage(id=doc["_id"], doc_id=doc.get("doc_id", ""), title=doc.get("title", ""),
                    summary=doc.get("summary", ""), body=doc.get("body", ""),
                    section=doc.get("section", ""), entities=doc.get("entities", []))


# ─────────────────────────── Entity ⇄ Neo4j Object (candidate) ───────────────────────────
def entity_to_object_candidate(e: Entity, source_tier: str, controlled: bool) -> dict:
    """Engine Entity → an Object CANDIDATE for the Action engine to commit.
    NOTE: this is a *candidate*, not a write. Ingest must route it through
    CreateObject / PromoteToOntology Action so writes are audited + bitemporal."""
    return {
        "id": e.id, "objectType": e.type, "title": e.name,
        "aliases": e.aliases, "doc_ids": e.doc_ids,
        "sourceTier": source_tier, "controlled": controlled,
        "facts": [{"key": "name", "value": e.name, "controlled": controlled,
                   "sourceTier": source_tier, "confidence": 0.78, "asOf": "ingest"}],
    }


def relation_to_link_candidate(r: Relation) -> dict:
    return {"src": r.src, "dst": r.dst, "lt": r.lt, "doc_id": r.doc_id}


# ─────────────────────────── Candidate → API answer context ───────────────────────────
def candidate_to_context(c: RetrievalCandidate) -> dict:
    """RetrievalCandidate (post-funnel) → the context block the API returns to the frontend.
    Security (OPA/Vault) is applied SEPARATELY in adapter 2 — this is pure shape mapping."""
    return {
        "title": c.meta.get("doc_name", c.meta.get("doc_id", "")),
        "text": c.text, "parent_id": c.parent_id, "chunk_id": c.chunk_id,
        "confidence": round(c.confidence, 3),
        "controlled": bool(c.meta.get("controlled", False)),
        "doc_id": c.meta.get("doc_id", ""),
    }
