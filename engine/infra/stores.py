"""Pillar 3 stores: Wiki page store, knowledge graph (entities/relations + communities),
and a BM25 sparse index. Reference in-memory; prod = Mongo/Neo4j/Elasticsearch."""
from __future__ import annotations
import math, re
from collections import defaultdict
from common.models import WikiPage, Entity, Relation


# ── Wiki page store (parent spans) ──
class WikiStore:
    def __init__(self):
        self.pages: dict[str, WikiPage] = {}
    def upsert(self, pages): 
        for p in pages: self.pages[p.id] = p
    def get(self, pid): return self.pages.get(pid)


# ── Knowledge graph + community detection (for global queries) ──
class GraphStore:
    def __init__(self):
        self.entities: dict[str, Entity] = {}
        self.adj: dict[str, set] = defaultdict(set)     # name → neighbor names
        self.relations: list = []
        self.communities: dict[int, list] = {}          # community_id → [entity names]
        self.community_summary: dict[int, str] = {}
        self.entity_community: dict[str, int] = {}

    def upsert_entities(self, ents):
        for e in ents: self.entities[e.name] = e

    def add_relations(self, rels):
        for r in rels:
            self.relations.append(r)
            self.adj[r.src].add(r.dst); self.adj[r.dst].add(r.src)

    def neighbors(self, name, hops=1):
        seen, frontier = {name}, {name}
        for _ in range(hops):
            nxt = set()
            for n in frontier: nxt |= self.adj.get(n, set())
            frontier = nxt - seen; seen |= nxt
        return seen - {name}

    def detect_communities(self):
        """Connected-components clustering (stand-in for Leiden). Returns affected ids.
        Prod: Leiden hierarchical community detection (igraph/Memgraph)."""
        self.communities.clear(); self.entity_community.clear()
        seen, cid = set(), 0
        for start in self.adj:
            if start in seen: continue
            comp, stack = [], [start]
            while stack:
                n = stack.pop()
                if n in seen: continue
                seen.add(n); comp.append(n)
                stack += [x for x in self.adj.get(n, set()) if x not in seen]
            self.communities[cid] = comp
            for n in comp: self.entity_community[n] = cid
            cid += 1
        # isolated entities each own community
        for name in self.entities:
            if name not in self.entity_community:
                self.communities[cid] = [name]; self.entity_community[name] = cid; cid += 1
        return list(self.communities.keys())

    def summarize_communities(self, summarizer=None, only=None):
        """Generate (or update) per-community summaries. `only` = incremental set.
        Prod: LLM map-reduce summary; here a deterministic join."""
        targets = only if only is not None else list(self.communities.keys())
        for c in targets:
            members = self.communities.get(c, [])
            types = sorted({self.entities[m].type for m in members if m in self.entities})
            self.community_summary[c] = (summarizer(members) if summarizer
                else f"社区{c}：{len(members)} 实体，类型 {','.join(types)}。成员：{', '.join(members[:8])}")


# ── BM25 sparse index (precise term/keyword recall) ──
class BM25Index:
    def __init__(self, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs: dict[str, list] = {}          # chunk_id → tokens
        self.df: dict[str, int] = defaultdict(int)
        self.meta: dict[str, dict] = {}
        self.avg_len = 0.0

    def add_chunks(self, chunks):
        for c in chunks:
            toks = _tok(c.text)
            self.docs[c.id] = toks
            self.meta[c.id] = {"parent_id": c.parent_id, "text": c.text, "doc_id": c.doc_id, **c.meta}
            for t in set(toks): self.df[t] += 1
        self.avg_len = sum(len(d) for d in self.docs.values()) / max(1, len(self.docs))

    def search(self, query: str, k: int = 30, filt=None) -> list:
        q = _tok(query); N = len(self.docs)
        scores = {}
        for cid, toks in self.docs.items():
            if filt and not filt(self.meta[cid]): continue
            tf = defaultdict(int)
            for t in toks: tf[t] += 1
            s = 0.0
            for t in set(q):
                if t not in self.df: continue
                idf = math.log(1 + (N - self.df[t] + 0.5) / (self.df[t] + 0.5))
                denom = tf[t] + self.k1 * (1 - self.b + self.b * len(toks) / (self.avg_len or 1))
                s += idf * (tf[t] * (self.k1 + 1)) / (denom or 1)
            if s > 0: scores[cid] = s
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]
        return [(cid, sc, self.meta[cid]) for cid, sc in ranked]


def _tok(text):
    return re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", text.lower())
