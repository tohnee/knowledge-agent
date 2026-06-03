"""Pillar 1 worker — orchestrates the scalable ingest compile pipeline.

Flow (per doc):  dedup → classify(tier) → extract(multi-level) → batch-embed →
                 entity-resolve(incremental) → write stores → incremental community update

Key scaling properties demonstrated here:
  · §1.1 tiered extraction (Tier-C skips LLM)
  · §1.3 batch embedding
  · §1.4 incremental: only re-summarize communities touched by the new doc (not full rebuild)
"""
from __future__ import annotations
from common.models import Document, DocStatus, Tier
from engine.ingest.extract.classifier import classify
from engine.ingest.extract.extractor import Extractor
from engine.ingest.extract.embedding import Embedder, embed_chunks, EntityResolver
from engine.infra.vector_store import VectorStore
from engine.infra.stores import WikiStore, GraphStore, BM25Index


class IngestPipeline:
    def __init__(self, extractor: Extractor, embedder: Embedder):
        self.extractor = extractor
        self.embedder = embedder
        self.resolver = EntityResolver(embedder)
        self.vstore = VectorStore(quantize=True)
        self.wiki = WikiStore()
        self.graph = GraphStore()
        self.bm25 = BM25Index()
        self._seen_fingerprints: set = set()
        self.stats = {"ingested": 0, "deduped": 0, "by_tier": {"A": 0, "B": 0, "C": 0}}

    def ingest(self, doc: Document) -> dict:
        # 1) dedup
        fp = doc.fingerprint()
        if fp in self._seen_fingerprints:
            self.stats["deduped"] += 1
            return {"id": doc.id, "status": "deduped"}
        self._seen_fingerprints.add(fp)

        # 2) classify → tier (cost control)
        doc.tier = classify(doc)
        self.stats["by_tier"][doc.tier.value] += 1
        doc.status = DocStatus.PARSED

        # 3) extract multi-level products
        prod = self.extractor.extract(doc, doc.tier)
        doc.status = DocStatus.EXTRACTED

        # 4) batch-embed child chunks
        embed_chunks(self.embedder, prod["chunks"], batch_size=256)

        # 5) incremental entity resolution → which subgraph is touched
        res = self.resolver.resolve(prod["entities"])

        # 6) write to all stores (child vectors, parent wiki, sparse, graph)
        self.vstore.upsert_chunks(prod["chunks"])
        self.bm25.add_chunks(prod["chunks"])
        self.wiki.upsert(prod["wiki_pages"])
        # graph entities = canonical (post-resolution)
        canon = [self.resolver.canonical[cid] for cid in res["touched_ids"]
                 if cid in self.resolver.canonical]
        self.graph.upsert_entities(canon)
        self.graph.add_relations(prod["relations"])

        # 7) incremental community update — ONLY affected communities re-summarized
        affected = self.graph.detect_communities()
        touched_comms = {self.graph.entity_community[self.resolver.canonical[cid].name]
                         for cid in res["touched_ids"]
                         if cid in self.resolver.canonical
                         and self.resolver.canonical[cid].name in self.graph.entity_community}
        self.graph.summarize_communities(only=list(touched_comms))

        doc.status = DocStatus.LINKED
        self.stats["ingested"] += 1
        return {"id": doc.id, "tier": doc.tier.value, "status": "linked",
                "chunks": len(prod["chunks"]), "wiki_pages": len(prod["wiki_pages"]),
                "entities_merged": len(res["merged"]), "entities_created": len(res["created"]),
                "communities_resummarized": len(touched_comms)}

    def ingest_batch(self, docs: list) -> list:
        """Batch ingest. In prod each doc is a queue task across an HPA worker pool;
        here sequential but the embedding/LLM calls are already batched internally."""
        return [self.ingest(d) for d in docs]
