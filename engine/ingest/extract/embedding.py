"""Pillar 1 · §1.3 batch embedding + §1.4 incremental entity resolution.
- Embedding is a throughput workload → always batch (256/512 at a time).
- Entity resolution merges new entities into existing Objects via vector-NN + alias
  table, so incremental ingest doesn't create duplicates or trigger O(N^2) relink."""
from __future__ import annotations
import math, hashlib
from abc import ABC, abstractmethod
from common.models import Chunk, Entity


# ── Embedder: batch interface. Prod = BGE-M3/OpenAI; here a deterministic hash embed ──
class Embedder(ABC):
    dim: int
    @abstractmethod
    def embed_batch(self, texts: list) -> list: ...


class HashEmbedder(Embedder):
    """Deterministic bag-of-hashed-tokens embedding — network-free, stable, good enough
    to exercise ANN math and the whole funnel in tests. Swap for BGE-M3 in prod."""
    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed_batch(self, texts: list) -> list:
        return [self._embed(t) for t in texts]   # prod: real batched GPU call here

    def _embed(self, text: str) -> list:
        v = [0.0] * self.dim
        toks = _tokenize(text)
        for tok in toks:
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if (h >> 8) & 1 else -1.0
            v[idx] += sign
        # add char-trigram features for sub-word/CJK robustness
        for i in range(len(text) - 2):
            tri = text[i:i + 3]
            h = int(hashlib.md5(tri.encode()).hexdigest(), 16)
            v[h % self.dim] += 0.5
        return _l2norm(v)


def embed_chunks(embedder: Embedder, chunks: list, batch_size: int = 256) -> None:
    """Mutates chunks in place with embeddings, processed in batches."""
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        vecs = embedder.embed_batch([c.text for c in batch])
        for c, v in zip(batch, vecs):
            c.embedding = v


# ── Incremental Entity Resolution ──
class EntityResolver:
    """Merge incoming entities into a canonical store. Resolution order:
       1) alias table (deterministic)  2) exact name  3) vector near-neighbor (>thr)
    Tracks which Objects changed so the worker can relink ONLY the affected subgraph."""
    def __init__(self, embedder: Embedder, sim_threshold: float = 0.86):
        self.embedder = embedder
        self.thr = sim_threshold
        self.canonical: dict[str, Entity] = {}     # canonical_id → Entity
        self.alias_index: dict[str, str] = {}       # alias(lower) → canonical_id

    def add_alias(self, alias: str, canonical_id: str):
        self.alias_index[alias.lower()] = canonical_id

    def resolve(self, incoming: list) -> dict:
        """Returns {merged: [...], created: [...], touched_ids: set} for incremental relink."""
        touched, created, merged = set(), [], []
        # embed incoming names once (batch)
        names = [e.name for e in incoming]
        vecs = self.embedder.embed_batch(names) if names else []
        for e, v in zip(incoming, vecs):
            cid = self._match(e, v)
            if cid:                                  # merge into existing
                tgt = self.canonical[cid]
                if e.name.lower() not in (a.lower() for a in tgt.aliases) and e.name != tgt.name:
                    tgt.aliases.append(e.name)
                    self.alias_index[e.name.lower()] = cid
                tgt.doc_ids = list(set(tgt.doc_ids + e.doc_ids))
                touched.add(cid); merged.append(cid)
            else:                                    # new canonical entity
                e.embedding = v
                self.canonical[e.id] = e
                self.alias_index[e.name.lower()] = e.id
                touched.add(e.id); created.append(e.id)
        return {"merged": merged, "created": created, "touched_ids": touched}

    def _match(self, e: Entity, v: list):
        # 1) alias / exact
        if e.name.lower() in self.alias_index:
            return self.alias_index[e.name.lower()]
        # 2) vector NN over canonical store (same type only)
        best_id, best = None, self.thr
        for cid, c in self.canonical.items():
            if c.type != e.type or c.embedding is None:
                continue
            s = _cos(v, c.embedding)
            if s > best:
                best, best_id = s, cid
        return best_id


# ── math/util ──
def _tokenize(text: str) -> list:
    import re
    return re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", text.lower())


def _l2norm(v: list) -> list:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _cos(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))   # both pre-normalized
