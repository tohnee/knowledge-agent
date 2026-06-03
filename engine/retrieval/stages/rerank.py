"""Pillar 2 · Stage 2 (rerank) + Stage 3 (compress + confidence gate).

Stage 2: Cross-Encoder scores query×chunk JOINTLY (not independently like the bi-encoder),
fixing mis-ranks. Runs ONLY on the top-N candidates — never the full corpus. Highest-ROI step.

Stage 3: parent-span restore (return Wiki page, not fragment) + relevance compression +
confidence threshold gate (drop < threshold) as the anti-hallucination defense."""
from __future__ import annotations
from abc import ABC, abstractmethod
import re, math
from common.models import RetrievalCandidate
from engine.infra.stores import WikiStore


# ── Cross-Encoder interface. Prod = ms-marco-MiniLM cross-encoder; here a token-overlap
#    + semantic proxy that jointly considers query and chunk (good enough to reorder in tests).
class Reranker(ABC):
    @abstractmethod
    def score(self, query: str, text: str) -> float: ...


class LexicalCrossEncoder(Reranker):
    """Joint query×text scoring proxy: weighted term coverage + phrase proximity + exact
    rare-term boost. Deterministic, network-free. Swap for a real cross-encoder in prod."""
    def score(self, query: str, text: str) -> float:
        q = _tok(query); t = _tok(text)
        if not q or not t:
            return 0.0
        tset = set(t)
        # 1) coverage: fraction of query terms present
        covered = [w for w in q if w in tset]
        coverage = len(covered) / len(q)
        # 2) exact rare-term boost (codes/models/IDs): query tokens that look specific
        rare = [w for w in q if re.match(r"^[a-z]?\d", w) or len(w) <= 2]
        rare_hit = sum(1 for w in rare if w in tset) / (len(rare) or 1)
        # 3) proximity: are covered terms close together in text?
        positions = [i for i, w in enumerate(t) if w in set(q)]
        prox = 0.0
        if len(positions) >= 2:
            span = positions[-1] - positions[0] + 1
            prox = len(positions) / span
        return 0.6 * coverage + 0.25 * rare_hit + 0.15 * prox


def rerank(query: str, candidates: list, reranker: Reranker, top_k: int) -> list:
    """Joint-score top candidates, sort, attach confidence, return top_k."""
    for c in candidates:
        c.rerank_score = reranker.score(query, c.text)
    candidates.sort(key=lambda c: c.rerank_score, reverse=True)
    top = candidates[:top_k]
    # confidence = calibrated rerank score (sigmoid-ish) — used by the gate
    for c in top:
        c.confidence = 1.0 / (1.0 + math.exp(-6 * (c.rerank_score - 0.4)))
    return top


def compress_and_gate(candidates: list, wiki: WikiStore,
                      query: str, conf_threshold: float = 0.5,
                      return_parent: bool = True) -> dict:
    """Stage 3: drop low-confidence (anti-hallucination), restore parent spans,
    compress to relevant sentences. Returns grounding context + citations."""
    kept = [c for c in candidates if c.confidence >= conf_threshold]
    contexts, citations, seen_parents = [], [], set()
    for c in kept:
        controlled = bool(c.meta.get("controlled", False))
        doc_id = c.meta.get("doc_id", "")
        # parent-span restore: return the Wiki page (full context), dedup by parent
        if return_parent and c.parent_id and c.parent_id not in seen_parents:
            page = wiki.get(c.parent_id)
            if page:
                seen_parents.add(c.parent_id)
                snippet = _compress(page.body, query)
                contexts.append({"title": page.title, "text": snippet,
                                 "confidence": round(c.confidence, 3),
                                 "controlled": controlled, "doc_id": page.doc_id})
                citations.append({"doc_id": page.doc_id, "parent_id": page.id,
                                  "title": page.title, "confidence": round(c.confidence, 3)})
                continue
        # fallback: chunk-level
        contexts.append({"title": c.meta.get("doc_name", doc_id),
                         "text": _compress(c.text, query), "confidence": round(c.confidence, 3),
                         "controlled": controlled, "doc_id": doc_id})
        citations.append({"doc_id": doc_id, "chunk_id": c.chunk_id,
                          "confidence": round(c.confidence, 3)})
    return {"contexts": contexts, "citations": citations,
            "dropped_low_confidence": len(candidates) - len(kept),
            "grounded": len(kept) > 0}


def _compress(text: str, query: str, max_sents: int = 3) -> str:
    """Contextual compression: keep only sentences overlapping the query."""
    q = set(_tok(query))
    sents = re.split(r"(?<=[。.!?！？\n])", text)
    scored = []
    for s in sents:
        if not s.strip():
            continue
        overlap = len(q & set(_tok(s)))
        scored.append((overlap, s.strip()))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [s for ov, s in scored[:max_sents] if ov > 0] or [text[:200]]
    return " ".join(top)


def _tok(text):
    return re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", text.lower())
