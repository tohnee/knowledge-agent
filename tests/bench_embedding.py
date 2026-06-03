"""Embedding + HNSW recall benchmark.

Two parts:
  A. Embedder mechanics (interface, batching, Matryoshka truncation, fallback) — runs anywhere.
  B. HNSW recall@k with a SEMANTICALLY-CLUSTERED synthetic embedder (controlled similarity
     structure that mimics real embeddings far better than the hash stand-in), proving HNSW
     hits target recall and exposing the ef_search recall/speed knob.

NOTE on BGE-M3: this sandbox firewalls huggingface.co, so the real model can't download here.
`bge_embedder.STEmbedder/BGEM3Embedder` are real and gracefully fall back to hash when the
model is unavailable. Final recall numbers must be confirmed on YOUR GPU box where BGE-M3
loads — the mechanics below are what's verifiable in CI.

Run:  python -m tests.bench_embedding
"""
import sys, math, random
from engine.ingest.extract.embedding import Embedder, HashEmbedder, _l2norm
from engine.ingest.extract.bge_embedder import BGEM3Embedder
from engine.infra.vector_store import VectorStore

random.seed(7)
P, F = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
res = []
def ck(n, c, d=""):
    res.append(c); print(f"  {P if c else F} {n}" + (f"  ({d})" if d else ""))


# ── A. embedder mechanics ──
print("\n=== A. Embedder mechanics ===")
bge = BGEM3Embedder(truncate_dim=256, allow_fallback=True)
bge._ensure()
print(f"  BGE-M3 backend resolved to: {bge.backend}")
vs = bge.embed_batch(["代工厂A 的 N3 产能", "EUV 光刻设备供应", "HBM 存储标准"])
ck("embed_batch returns one vector per text", len(vs) == 3)
ck("vectors are truncated to dim", all(len(v) == bge.dim for v in vs), f"dim={bge.dim}")
ck("vectors L2-normalized", all(abs(sum(x*x for x in v) - 1.0) < 1e-6 for v in vs))
ck("graceful fallback when model absent (sandbox)", "fallback" in bge.backend or bge.backend.startswith("bge"))
# Matryoshka: 128 vs 512 both work
small = BGEM3Embedder(truncate_dim=128, allow_fallback=True); small._ensure()
ck("Matryoshka truncation honored (128)", small.dim == 128, f"dim={small.dim}")


# ── B. HNSW recall with semantically-clustered vectors (models real embeddings) ──
class ClusterEmbedder(Embedder):
    """Synthetic embedder with REAL semantic structure: texts sharing a (node,topic) land
    near a shared cluster centroid + small noise. Far better proxy for BGE-M3 than the hash
    embedder (which has near-orthogonal vectors). Lets us measure recall honestly."""
    def __init__(self, dim=128, noise=0.25):
        self.dim = dim; self.noise = noise; self._centroids = {}
        self._rng = random.Random(11)
    def _centroid(self, key):
        if key not in self._centroids:
            self._centroids[key] = _l2norm([self._rng.gauss(0, 1) for _ in range(self.dim)])
        return self._centroids[key]
    def embed_batch(self, texts):
        out = []
        for t in texts:
            key = _key(t)
            c = self._centroid(key)
            v = [c[i] + self._rng.gauss(0, self.noise) for i in range(self.dim)]
            out.append(_l2norm(v))
        return out

def _key(t):
    import re
    n = (re.search(r"N\d+", t) or [None])
    n = re.search(r"N\d+", t); node = n.group(0) if n else "X"
    topic = next((tp for tp in ["产能","良率","营收","扩产","供应","封装","光刻"] if tp in t), "X")
    return (node, topic)

print("\n=== B. HNSW recall@10 (semantically-clustered vectors) ===")
emb = ClusterEmbedder(dim=128)
topics = ["产能","良率","营收","扩产","供应","封装","光刻"]; nodes = ["N2","N3","N5","N7"]
docs = [f"代工厂A 的 {random.choice(nodes)} 节点 {random.choice(topics)} 分析 文档{i}" for i in range(600)]
vecs = emb.embed_batch(docs)

bf = VectorStore(quantize=False, use_hnsw=False)
hn = VectorStore(quantize=False, use_hnsw=True)
for i, v in enumerate(vecs):
    m = {"text": docs[i], "parent_id": f"p{i}", "doc_id": f"d{i}"}
    bf._shard_for(m).add(f"c{i}", v, m); hn._shard_for(m).add(f"c{i}", v, m)

qtexts = [f"{random.choice(nodes)} 节点 {random.choice(topics)} 怎么样" for _ in range(80)]
qvecs = emb.embed_batch(qtexts)
K = 10; recalls = []
for qv in qvecs:
    gt = {cid for cid, _, _ in bf.search(qv, k=K)}      # exact (brute-force) ground truth
    got = {cid for cid, _, _ in hn.search(qv, k=K)}     # HNSW approximate
    recalls.append(len(gt & got) / max(1, len(gt)))
recall = sum(recalls) / len(recalls)
print(f"  corpus={len(docs)} vectors, dim={emb.dim}, K={K}")
ck("HNSW recall@10 ≥ 0.90 (target)", recall >= 0.90, f"recall={recall:.3f}")

# also show quantized store keeps recall
hnq = VectorStore(quantize=True, use_hnsw=True)
for i, v in enumerate(vecs):
    m = {"text": docs[i], "parent_id": f"p{i}", "doc_id": f"d{i}"}
    hnq._shard_for(m).add(f"c{i}", v, m)
rq = []
for qv in qvecs:
    gt = {cid for cid, _, _ in bf.search(qv, k=K)}
    got = {cid for cid, _, _ in hnq.search(qv, k=K)}
    rq.append(len(gt & got) / max(1, len(gt)))
recall_q = sum(rq) / len(rq)
ck("HNSW + int8 quantization keeps recall ≥ 0.88", recall_q >= 0.88, f"recall={recall_q:.3f}")

print("\n" + "=" * 52)
ok = sum(res)
print(f"  EMBEDDING/RECALL: {ok}/{len(res)} passed")
print("  (Real BGE-M3 numbers: confirm on your GPU box; HF is firewalled in this sandbox.)")
print("=" * 52)
sys.exit(0 if ok == len(res) else 1)
