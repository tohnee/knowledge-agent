"""Pillar 2 · Stage 1 — Hybrid recall with RRF fusion.
Dense ANN (semantic) ∥ BM25 (precise terms) run in parallel, each returns top-N,
then Reciprocal Rank Fusion unions them. Dense alone misses exact terms (N3/JESD79);
BM25 alone misses synonyms. Together = the precision floor at scale."""
from __future__ import annotations
from common.models import RetrievalCandidate
from engine.infra.vector_store import VectorStore
from engine.infra.stores import BM25Index
from engine.retrieval.stages.router import QueryPlan


def hybrid_recall(plan: QueryPlan, query_vec: list,
                  vstore: VectorStore, bm25: BM25Index,
                  rrf_k: int = 60) -> list:
    """Returns fused, deduped list[RetrievalCandidate] (top_recall)."""
    filt = _make_filter(plan.filters)

    dense = vstore.search(query_vec, k=plan.top_recall, shard_keys=plan.shard_keys or None, filt=filt)
    sparse = bm25.search(plan.query, k=plan.top_recall, filt=filt)

    # rank positions for RRF
    dense_rank  = {cid: i for i, (cid, _, _) in enumerate(dense)}
    sparse_rank = {cid: i for i, (cid, _, _) in enumerate(sparse)}
    meta_of, dense_s, sparse_s = {}, {}, {}
    for cid, s, m in dense:  meta_of[cid] = m; dense_s[cid] = s
    for cid, s, m in sparse: meta_of.setdefault(cid, m); sparse_s[cid] = s

    all_ids = set(dense_rank) | set(sparse_rank)
    fused = []
    for cid in all_ids:
        rrf = 0.0
        if cid in dense_rank:  rrf += 1.0 / (rrf_k + dense_rank[cid])
        if cid in sparse_rank: rrf += 1.0 / (rrf_k + sparse_rank[cid])
        m = meta_of[cid]
        fused.append(RetrievalCandidate(
            chunk_id=cid, text=m.get("text", ""), parent_id=m.get("parent_id", ""),
            dense_score=dense_s.get(cid, 0.0), sparse_score=sparse_s.get(cid, 0.0),
            rrf_score=rrf, meta=m))
    fused.sort(key=lambda c: c.rrf_score, reverse=True)
    return fused[:plan.top_recall]


def _make_filter(filters: dict):
    if not filters:
        return None
    def f(meta):
        for k, v in filters.items():
            if meta.get(k) != v:
                return False
        return True
    return f
