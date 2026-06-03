"""Pillar 3 · §3.1 Vector index — ANN + int8 scalar quantization + sharding.
This is an in-memory, dependency-free reference implementation that demonstrates the
REAL engineering knobs (quantization, sharding, recall/speed tradeoff). In production
this maps to Qdrant (≤10M) or Milvus IVF-PQ (≥100M); the API mirrors theirs."""
from __future__ import annotations
import math, struct
from common.models import Chunk


# ── int8 Scalar Quantization: 4× memory cut, <1% accuracy loss (the free lunch) ──
class ScalarQuantizer:
    """Per-vector symmetric int8 quantization. Stores int8 codes + one float scale."""
    @staticmethod
    def encode(vec: list) -> tuple:
        amax = max((abs(x) for x in vec), default=1e-9) or 1e-9
        scale = amax / 127.0
        codes = bytes((max(-127, min(127, round(x / scale))) & 0xFF) for x in vec)
        return codes, scale

    @staticmethod
    def decode(codes: bytes, scale: float) -> list:
        return [((c - 256) if c > 127 else c) * scale for c in codes]


class Shard:
    """One shard = independent ANN search space (tenant/department partition).
    Set use_hnsw=True for O(log n) ANN (the production path); False = brute-force O(n)."""
    def __init__(self, name: str, quantize: bool = True, use_hnsw: bool = False):
        self.name = name
        self.quantize = quantize
        self.use_hnsw = use_hnsw
        self.codes: dict[str, tuple] = {}    # id → (int8 bytes, scale)  (quantized store)
        self.raw: dict[str, list] = {}        # id → float vec (kept only if quantize=False)
        self.meta: dict[str, dict] = {}
        self._hnsw = None
        if use_hnsw:
            from engine.infra.hnsw import HNSW
            self._hnsw = HNSW()

    def add(self, cid: str, vec: list, meta: dict):
        if self.quantize:
            self.codes[cid] = ScalarQuantizer.encode(vec)
        else:
            self.raw[cid] = vec
        self.meta[cid] = meta
        if self._hnsw is not None:
            self._hnsw.add(cid, _l2norm(vec))

    def _vec(self, cid: str) -> list:
        if self.quantize:
            codes, scale = self.codes[cid]
            return ScalarQuantizer.decode(codes, scale)
        return self.raw[cid]

    def _ids(self):
        return self.codes.keys() if self.quantize else self.raw.keys()

    def search(self, q: list, k: int, filt=None) -> list:
        """HNSW (O(log n)) when enabled, else brute-force (O(n), exact).
        Returns [(id, score, meta)]. `filt(meta)->bool` does metadata pre-filtering."""
        qn = _l2norm(q)
        if self._hnsw is not None:
            if filt is None:
                # fast path: HNSW ANN (over-fetch a bit for recall, then trim)
                hits = self._hnsw.search(qn, max(k, 30))
                return [(cid, sim, self.meta[cid]) for cid, sim in hits[:k]]
            # Filter-aware ANN path: over-fetch from HNSW, apply metadata security/tenant
            # filters, then fall back to exact scan only if ANN cannot produce enough
            # eligible rows. This keeps filtered queries on the ANN path for common cases.
            fetch = min(len(self.meta), max(k * 8, 100))
            hits = self._hnsw.search(qn, fetch)
            filtered = [(cid, sim, self.meta[cid]) for cid, sim in hits if filt(self.meta[cid])]
            if len(filtered) >= k or fetch >= len(self.meta):
                return filtered[:k]
        # exact fallback
        scored = []
        for cid in self._ids():
            m = self.meta[cid]
            if filt and not filt(m):
                continue
            scored.append((cid, _cos(qn, _l2norm(self._vec(cid))), m))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def memory_bytes(self) -> int:
        if self.quantize:
            return sum(len(c) + 4 for c, _ in self.codes.values())   # int8 + float scale
        return sum(len(v) * 4 for v in self.raw.values())            # float32


class VectorStore:
    """Sharded vector store. Routes by shard_key (e.g. department) → smaller candidate
    domain + parallelism. Merges per-shard results with a heap (here: sort)."""
    def __init__(self, quantize: bool = True, use_hnsw: bool = False):
        self.quantize = quantize
        self.use_hnsw = use_hnsw
        self.shards: dict[str, Shard] = {}

    def _shard_for(self, meta: dict) -> Shard:
        key = meta.get("department", "default")
        if key not in self.shards:
            self.shards[key] = Shard(key, self.quantize, self.use_hnsw)
        return self.shards[key]

    def upsert_chunks(self, chunks: list):
        for c in chunks:
            if c.embedding is None:
                continue
            self._shard_for(c.meta).add(c.id, c.embedding,
                {"parent_id": c.parent_id, "text": c.text, "doc_id": c.doc_id, **c.meta})

    def search(self, q: list, k: int = 30, shard_keys=None, filt=None) -> list:
        targets = ([self.shards[s] for s in shard_keys if s in self.shards]
                   if shard_keys else list(self.shards.values()))
        merged = []
        for sh in targets:                                   # each shard independent
            merged += sh.search(q, k, filt)
        merged.sort(key=lambda x: x[1], reverse=True)
        return merged[:k]

    def stats(self) -> dict:
        return {"shards": len(self.shards),
                "vectors": sum(len(s.codes if s.quantize else s.raw) for s in self.shards.values()),
                "memory_bytes": sum(s.memory_bytes() for s in self.shards.values()),
                "quantized": self.quantize}


def _l2norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))
