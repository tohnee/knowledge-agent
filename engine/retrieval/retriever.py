"""Pillar 2 orchestrator — runs the full retrieval funnel end to end.

local:  route → hybrid recall(ANN+BM25+RRF) → cross-encoder rerank → compress + gate
global: route → community summaries (map-reduce over pre-compiled summaries)

Returns grounding context for the generation step, with per-stage timing for observability."""
from __future__ import annotations
import time
from engine.retrieval.stages.router import route, QueryPlan
from engine.retrieval.stages.recall import hybrid_recall
from engine.retrieval.stages.rerank import rerank, compress_and_gate, Reranker
from engine.ingest.extract.embedding import Embedder
from engine.infra.vector_store import VectorStore
from engine.infra.stores import BM25Index, WikiStore, GraphStore


class Retriever:
    def __init__(self, embedder: Embedder, reranker: Reranker,
                 vstore: VectorStore, bm25: BM25Index,
                 wiki: WikiStore, graph: GraphStore):
        self.embedder = embedder
        self.reranker = reranker
        self.vstore = vstore
        self.bm25 = bm25
        self.wiki = wiki
        self.graph = graph
        # L1 semantic query cache
        self._cache: dict[str, dict] = {}

    def clear_cache(self) -> None:
        """Invalidate cached retrieval results after ingest or governed writes."""
        self._cache.clear()

    def retrieve(self, query: str, role: str = "analyst",
                 department: str | None = None) -> dict:
        t0 = time.perf_counter()
        timing = {}

        # L1 cache
        ck = f"{role}|{department}|{query}"
        if ck in self._cache:
            out = dict(self._cache[ck]); out["cache_hit"] = True
            return out

        plan = route(query, role=role, department=department)
        timing["route_ms"] = _ms(t0)

        if plan.mode == "global":
            result = self._global(plan)
        else:
            result = self._local(plan, timing)

        result["mode"] = plan.mode
        result["filters"] = plan.filters
        result["total_ms"] = _ms(t0)
        result["timing"] = timing
        result["cache_hit"] = False
        self._cache[ck] = result
        return result

    def _local(self, plan: QueryPlan, timing: dict) -> dict:
        # embed query (single)
        t = time.perf_counter()
        qvec = self.embedder.embed_batch([plan.query])[0]
        timing["embed_ms"] = _ms(t)

        # Stage 1 hybrid recall
        t = time.perf_counter()
        cands = hybrid_recall(plan, qvec, self.vstore, self.bm25)
        timing["recall_ms"] = _ms(t); timing["recall_candidates"] = len(cands)

        # Stage 2 rerank (only on candidates)
        t = time.perf_counter()
        top = rerank(plan.query, cands, self.reranker, plan.top_rerank)
        timing["rerank_ms"] = _ms(t)

        # Stage 3 compress + confidence gate
        t = time.perf_counter()
        ctx = compress_and_gate(top, self.wiki, plan.query,
                                conf_threshold=plan.conf_threshold)
        timing["compress_ms"] = _ms(t)
        return ctx

    def _global(self, plan: QueryPlan) -> dict:
        """Global question → read pre-compiled community summaries (no full-corpus scan)."""
        summaries = []
        for cid, summ in self.graph.community_summary.items():
            members = self.graph.communities.get(cid, [])
            score = self.reranker.score(plan.query, summ + " " + " ".join(members))
            summaries.append((score, cid, summ))
        summaries.sort(key=lambda x: x[0], reverse=True)
        top = summaries[:5]
        return {"contexts": [{"title": f"community_{cid}", "text": summ,
                              "confidence": round(min(1.0, sc), 3)} for sc, cid, summ in top],
                "citations": [{"community_id": cid} for _, cid, _ in top],
                "grounded": len(top) > 0, "dropped_low_confidence": 0}


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000, 3)
