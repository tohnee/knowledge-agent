"""WKA · BGE-M3 embedder — production multilingual embeddings (中英日韩术语跨语).
Implements the same `Embedder` interface as HashEmbedder, so it's a drop-in for
`System(embedder=...)`. This is what lifts HNSW recall to target (>0.9): BGE-M3 vectors
are well-separated, unlike the deterministic hash stand-in used in tests.

Features from the scaling plan §3.2:
  · batched encoding (throughput; embedding is GPU-throughput-bound)
  · Matryoshka (MRL) dimension truncation — keep first N dims, big storage/compute cut, tiny loss
  · L2-normalized output (cosine = dot product, matches the vector store)

Graceful fallback: if FlagEmbedding/sentence-transformers/torch aren't installed (e.g. CI),
falls back to HashEmbedder so the pipeline still runs — with a clear flag."""
from __future__ import annotations
import math
from engine.ingest.extract.embedding import Embedder, HashEmbedder, _l2norm


class BGEM3Embedder(Embedder):
    """BGE-M3 dense embeddings. Lazy-loads the model on first use.
    `truncate_dim`: Matryoshka — keep the first N dims (e.g. 256/512/1024). None = full (1024)."""

    def __init__(self, model_name: str = "BAAI/bge-m3", truncate_dim: int | None = 512,
                 device: str | None = None, batch_size: int = 64,
                 use_fp16: bool = True, allow_fallback: bool = True):
        self.model_name = model_name
        self.truncate_dim = truncate_dim
        self.device = device
        self.batch_size = batch_size
        self.use_fp16 = use_fp16
        self.allow_fallback = allow_fallback
        self._model = None
        self._fallback = None
        self.dim = truncate_dim or 1024
        self.backend = "uninitialized"

    def _ensure(self):
        if self._model is not None or self._fallback is not None:
            return
        try:
            from FlagEmbedding import BGEM3FlagModel          # pip install FlagEmbedding
            self._model = BGEM3FlagModel(self.model_name, use_fp16=self.use_fp16,
                                         device=self.device)
            self.backend = "bge-m3"
            # discover full dim once
            probe = self._model.encode(["x"], batch_size=1)["dense_vecs"][0]
            full = len(probe)
            self.dim = min(self.truncate_dim or full, full)
        except Exception as e:
            if not self.allow_fallback:
                raise
            self._fallback = HashEmbedder(dim=self.truncate_dim or 256)
            self.dim = self._fallback.dim
            self.backend = f"fallback-hash ({type(e).__name__})"

    def embed_batch(self, texts: list) -> list:
        self._ensure()
        if self._fallback is not None:
            return self._fallback.embed_batch(texts)
        out = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            vecs = self._model.encode(batch, batch_size=len(batch),
                                      max_length=8192)["dense_vecs"]
            for v in vecs:
                v = list(v[: self.dim]) if self.truncate_dim else list(v)   # Matryoshka truncation
                out.append(_l2norm(v))
        return out


def make_embedder(prefer_bge: bool = True, truncate_dim: int = 512,
                  hash_dim: int = 256) -> Embedder:
    """Factory: try BGE-M3, else HashEmbedder. Used by System/composition root.
    Returns an Embedder whose `.backend` tells you which one you got."""
    if prefer_bge:
        emb = BGEM3Embedder(truncate_dim=truncate_dim, allow_fallback=True)
        emb._ensure()
        return emb
    return HashEmbedder(dim=hash_dim)


class STEmbedder(Embedder):
    """sentence-transformers backend — works with any ST multilingual model.
    Used to PROVE recall with real embeddings when FlagEmbedding/BGE-M3 isn't available.
    e.g. 'paraphrase-multilingual-MiniLM-L12-v2' (small, multilingual)."""
    def __init__(self, model_name="paraphrase-multilingual-MiniLM-L12-v2",
                 truncate_dim=None, batch_size=64, allow_fallback=True):
        self.model_name = model_name; self.truncate_dim = truncate_dim
        self.batch_size = batch_size; self.allow_fallback = allow_fallback
        self._model = None; self._fallback = None; self.dim = truncate_dim or 384
        self.backend = "uninitialized"
    def _ensure(self):
        if self._model is not None or self._fallback is not None: return
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name)
            full = self._model.get_sentence_embedding_dimension()
            self.dim = min(self.truncate_dim or full, full)
            self.backend = "sentence-transformers:" + self.model_name
        except Exception as e:
            if not self.allow_fallback: raise
            self._fallback = HashEmbedder(dim=self.truncate_dim or 256)
            self.dim = self._fallback.dim; self.backend = f"fallback-hash ({type(e).__name__})"
    def embed_batch(self, texts):
        self._ensure()
        if self._fallback is not None: return self._fallback.embed_batch(texts)
        vecs = self._model.encode(texts, batch_size=self.batch_size, normalize_embeddings=False)
        out = []
        for v in vecs:
            v = list(v[: self.dim]) if self.truncate_dim else list(v)
            out.append(_l2norm([float(x) for x in v]))
        return out
