"""WKA-Fused · Composition Root
================================================================================
Builds the single shared object graph so ingest and retrieval use the SAME engine
stores (vector/bm25/graph/wiki) and the SAME governed KnowledgeStore + Action engine.

This is the wiring that makes the three layers one system:
    GovernedIngest ──writes──▶ KnowledgeStore (via Action engine)
          │ owns                         ▲
          ▼ shares                       │ reads (security applied)
    engine stores (vector/bm25/graph/wiki)
          ▲ reads
          │
    Retriever ──▶ GroundedQA (OPA/Vault) ──▶ API ──▶ frontend
"""
from __future__ import annotations
from engine.ingest.extract.extractor import StubExtractor
from engine.ingest.extract.embedding import HashEmbedder
from engine.retrieval.retriever import Retriever
from engine.retrieval.stages.rerank import LexicalCrossEncoder
from action_engine.engine import ActionEngine
from action_engine.store import InMemoryKnowledgeStore
from adapters.governed_ingest import GovernedIngest
from api.services_ask import GroundedQA


def _build_store(backend, neo4j_driver=None):
    """Pick the KnowledgeStore backend. 'memory' (default) or 'neo4j'.
    Same contract either way (KnowledgeStoreBase) — the rest of the system is unchanged."""
    if backend == "neo4j":
        from action_engine.store_neo4j import Neo4jKnowledgeStore
        return Neo4jKnowledgeStore(driver=neo4j_driver)
    return InMemoryKnowledgeStore()


class System:
    """The fully-wired WKA. Construct once, share everywhere.
    `store_backend`: 'memory' (tests/headless) or 'neo4j' (production)."""
    def __init__(self, embed_dim: int = 256, use_hnsw: bool = True,
                 store_backend: str = "memory", neo4j_driver=None, extractor=None,
                 embedder=None, prefer_bge: bool = False):
        # shared infra
        if embedder is not None:
            self.embedder = embedder
        elif prefer_bge:
            from engine.ingest.extract.bge_embedder import make_embedder
            self.embedder = make_embedder(prefer_bge=True, truncate_dim=512)  # prod: BGE-M3
        else:
            self.embedder = HashEmbedder(dim=embed_dim)     # tests: zero-dep
        self.reranker = LexicalCrossEncoder()               # prod: ms-marco cross-encoder
        self.store = _build_store(store_backend, neo4j_driver)   # prod: Neo4j
        self.actions = ActionEngine(self.store)             # the only write channel

        # extraction engine — StubExtractor (default, zero-dep) or AgentExtractor
        # (Claude Code + DeepSeek/GLM). Swappable without touching the rest of the system.
        extractor = extractor or StubExtractor()

        # ingest (governed — writes ontology via Action)
        self.ingest = GovernedIngest(extractor, self.embedder,
                                     self.actions, self.store)
        # enable HNSW O(log N) ANN on the engine vector store (scaling §3.1)
        self.ingest.pipe.vstore.use_hnsw = use_hnsw

        # retrieval shares the SAME engine stores the ingest filled
        self.retriever = Retriever(self.embedder, self.reranker,
                                   self.ingest.vstore, self.ingest.bm25,
                                   self.ingest.wiki, self.ingest.graph)
        self.qa = GroundedQA(self.retriever)

    # convenience pass-throughs used by the API/tests
    def ingest_doc(self, doc): return self.ingest.ingest(doc)
    def ask(self, q, role="analyst", dept=None): return self.qa.answer(q, role, dept)
    def run_action(self, name, params, role, confirmed=False):
        return self.actions.execute(name, params, role, confirmed)
