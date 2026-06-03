"""ADAPTER 3 · Ingest → Action
================================================================================
The engine's IngestPipeline produces candidate Objects/Links/WikiPages and fills the
in-engine vector/graph stores. But in WKA, **the ontology is only written through the
Action engine** (audit + bitemporal + permission). So ingest must NOT write Objects/Links
directly — it routes them through Action.

This adapter wraps the proven IngestPipeline:
  1. run the engine pipeline (chunks→Qdrant, wiki→stays, entities resolved)  ← engine owns recall structures
  2. for each resolved Entity  → CreateObject Action  (audited write to KnowledgeStore)
  3. for each Relation         → CreateLink   Action
  4. each WikiPage             → KnowledgeStore.put_wiki (parent span for answers)
  5. controlled signal         → enqueue MarkExportControlled (compliance待审, NOT auto)

Result: the engine keeps the fast retrieval indexes; the business store keeps the
governed ontology; the two stay consistent because every Object write went through Action."""
from __future__ import annotations
from common.models import Document
from engine.ingest.workers.pipeline import IngestPipeline
from engine.ingest.extract.extractor import Extractor
from engine.ingest.extract.embedding import Embedder
from adapters.model_map import entity_to_object_candidate, relation_to_link_candidate, wiki_to_mongo


class GovernedIngest:
    """Ingest that writes the ontology ONLY through the Action engine."""
    def __init__(self, extractor: Extractor, embedder: Embedder, action_engine, store,
                 ingest_role: str = "analyst"):
        self.pipe = IngestPipeline(extractor, embedder)   # engine owns vector/bm25/graph/wiki
        self.actions = action_engine
        self.store = store
        self.role = ingest_role
        self.pending_controls: list = []                   # candidate MarkExportControlled

    # expose engine stores for the retriever (shared instances)
    @property
    def vstore(self): return self.pipe.vstore
    @property
    def bm25(self): return self.pipe.bm25
    @property
    def graph(self): return self.pipe.graph
    @property
    def wiki(self): return self.pipe.wiki

    def ingest(self, doc: Document) -> dict:
        # 1) run the proven engine pipeline (recall structures get populated here)
        summary = self.pipe.ingest(doc)
        if summary["status"] == "deduped":
            return summary

        # 2) commit resolved entities as Object CANDIDATES via Action (audited)
        objs_created = objs_merged = links = 0
        for cid, ent in self.pipe.resolver.canonical.items():
            # only push entities that belong to THIS doc (incremental)
            if doc.id not in ent.doc_ids:
                continue
            cand = entity_to_object_candidate(ent, doc.source_tier.value, doc.controlled)
            res = self.actions.execute("create_object", cand, role=self.role)
            if "created" in res["result"]: objs_created += 1
            elif "merged" in res["result"]: objs_merged += 1

        # 3) commit relations via Action
        for r in self._doc_relations(doc):
            self.actions.execute("create_link", relation_to_link_candidate(r), role=self.role)
            links += 1

        # 4) wiki pages → business store (parent spans for grounded answers)
        for page in self.pipe.wiki.pages.values():
            if page.doc_id == doc.id:
                self.store.put_wiki(wiki_to_mongo(page, controlled=doc.controlled))

        # 5) controlled signal → MarkExportControlled candidate (compliance待审, not auto-committed)
        if doc.controlled:
            self.pending_controls.append({"entityId": doc.id, "doc": doc.name,
                                          "reason": "controlled document ingested"})

        summary.update({"objects_created": objs_created, "objects_merged": objs_merged,
                        "links_committed": links, "via": "action_engine"})
        return summary

    def ingest_batch(self, docs):
        return [self.ingest(d) for d in docs]

    def _doc_relations(self, doc):
        # relations the engine extracted for this doc (co-occurrence)
        return [r for r in self.pipe.graph.relations if r.doc_id == doc.id]
