"""Pillar 3 · §3.1 HNSW index — a compact, dependency-free implementation that delivers
real O(log N) approximate nearest-neighbor search (vs brute-force O(N)).

This is a faithful (if minimal) implementation of Hierarchical Navigable Small World:
multi-layer graph, greedy descent from sparse top layer, ef-search beam at layer 0.
Production: use Qdrant/Milvus/FAISS HNSW. This proves the algorithm + the speedup."""
from __future__ import annotations
import math, random, heapq


def _cos_dist(a, b):           # both are L2-normalized → distance = 1 - cos
    return 1.0 - sum(x * y for x, y in zip(a, b))


class HNSW:
    def __init__(self, M: int = 16, ef_construction: int = 200, ef_search: int = 200, seed: int = 42):
        self.M = M
        self.Mmax0 = M * 2
        self.efc = ef_construction
        self.ef = ef_search
        self.ml = 1.0 / math.log(M)
        self.rng = random.Random(seed)
        self.vectors: dict = {}                  # id → vec
        self.layers: list = []                   # list[ dict[id → set(neighbor_ids)] ]
        self.entry = None
        self.top = -1

    def _level(self) -> int:
        return int(-math.log(self.rng.random()) * self.ml)

    def add(self, node_id, vec):
        self.vectors[node_id] = vec
        lvl = self._level()
        while len(self.layers) <= lvl:
            self.layers.append({})
        for l in range(lvl + 1):
            self.layers[l].setdefault(node_id, set())

        if self.entry is None:
            self.entry, self.top = node_id, lvl
            return

        # descend from top to lvl+1 with greedy search (ef=1)
        cur = self.entry
        for l in range(self.top, lvl, -1):
            cur = self._greedy(vec, cur, l)
        # connect at each layer from min(lvl,top) down to 0
        for l in range(min(lvl, self.top), -1, -1):
            cands = self._search_layer(vec, [cur], l, self.efc)
            neighbors = [c for _, c in heapq.nsmallest(self.M, cands)]
            mmax = self.Mmax0 if l == 0 else self.M
            for nb in neighbors:
                self.layers[l][node_id].add(nb)
                self.layers[l][nb].add(node_id)
                # prune over-connected nodes
                if len(self.layers[l][nb]) > mmax:
                    self._prune(nb, l, mmax)
            cur = neighbors[0] if neighbors else cur
        if lvl > self.top:
            self.entry, self.top = node_id, lvl

    def _prune(self, node, l, mmax):
        v = self.vectors[node]
        ranked = sorted(self.layers[l][node], key=lambda x: _cos_dist(v, self.vectors[x]))
        self.layers[l][node] = set(ranked[:mmax])

    def _greedy(self, q, entry, l):
        best, best_d = entry, _cos_dist(q, self.vectors[entry])
        improved = True
        while improved:
            improved = False
            for nb in self.layers[l].get(best, ()):
                d = _cos_dist(q, self.vectors[nb])
                if d < best_d:
                    best, best_d, improved = nb, d, True
        return best

    def _search_layer(self, q, entries, l, ef):
        visited = set(entries)
        cand = [(_cos_dist(q, self.vectors[e]), e) for e in entries]
        heapq.heapify(cand)
        results = [(-d, e) for d, e in cand]
        heapq.heapify(results)
        while cand:
            d, c = heapq.heappop(cand)
            if results and -results[0][0] < d and len(results) >= ef:
                break
            for nb in self.layers[l].get(c, ()):
                if nb in visited:
                    continue
                visited.add(nb)
                dn = _cos_dist(q, self.vectors[nb])
                if len(results) < ef or dn < -results[0][0]:
                    heapq.heappush(cand, (dn, nb))
                    heapq.heappush(results, (-dn, nb))
                    if len(results) > ef:
                        heapq.heappop(results)
        return [(-negd, e) for negd, e in results]

    def search(self, q, k: int):
        if self.entry is None:
            return []
        cur = self.entry
        for l in range(self.top, 0, -1):
            cur = self._greedy(q, cur, l)
        found = self._search_layer(q, [cur], 0, max(self.ef, k))
        found.sort(key=lambda x: x[0])
        return [(e, 1.0 - d) for d, e in found[:k]]   # return (id, similarity)
