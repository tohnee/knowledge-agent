"""WKA business layer · In-memory KnowledgeStore.
Zero-dependency reference backing the Action engine — used by tests and the headless
closed loop. Production swaps in Neo4jKnowledgeStore (identical contract).

`KnowledgeStore` stays as an alias so existing imports keep working."""
from __future__ import annotations
import time
from action_engine.store_base import KnowledgeStoreBase


class InMemoryKnowledgeStore(KnowledgeStoreBase):
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.links: list = []
        self.wiki: dict[str, dict] = {}          # _id → wiki doc
        self.capacity_obs: dict[str, list] = {}  # objId → [observation,...] (bitemporal)
        self.status_events: list = []
        self.audit_events: list = []

    # ── Objects ──
    def get_object(self, oid): return self.objects.get(oid)

    def put_object(self, obj: dict):
        self.objects[obj["id"]] = obj

    def merge_object(self, existing: dict, incoming: dict):
        existing.setdefault("aliases", [])
        for a in incoming.get("aliases", []):
            if a not in existing["aliases"]:
                existing["aliases"].append(a)
        existing["doc_ids"] = list(set(existing.get("doc_ids", []) + incoming.get("doc_ids", [])))

    def set_controlled(self, oid, val, eccn=""):
        o = self.objects.get(oid)
        if o:
            o["controlled"] = val
            o["eccn"] = eccn
            for f in o.get("facts", []):
                f["controlled"] = val
        for w in self.wiki.values():
            if w.get("doc_id") == oid or w.get("_id") == oid:
                w["controlled"] = val

    def bump_confidence(self, oid, delta):
        o = self.objects.get(oid)
        if o:
            o["confidence"] = min(1.0, o.get("confidence", 0.78) + delta)

    # ── Links ──
    def put_link(self, link: dict):
        self.links.append(link)

    # ── Wiki pages ──
    def put_wiki(self, doc: dict):
        self.wiki[doc["_id"]] = doc

    def get_wiki(self, pid): return self.wiki.get(pid)

    # ── Bitemporal capacity (append-only, supersededBy) ──
    def append_capacity(self, obj_id, wspm, valid_time, source_tier, confidence):
        obs = self.capacity_obs.setdefault(obj_id, [])
        for o in obs:
            if o["supersededBy"] is None and o["validTime"] == valid_time:
                o["supersededBy"] = f"obs_{len(obs)}"
        obs.append({"capacityWSPM": wspm, "validTime": valid_time,
                    "transactionTime": time.strftime("%Y-%m-%d"),
                    "sourceTier": source_tier, "confidence": confidence,
                    "supersededBy": None})

    def capacity_asof(self, obj_id, year: int):
        obs = self.capacity_obs.get(obj_id, [])
        cand = [o for o in obs if int(str(o["validTime"])[:4]) <= year]
        return cand[-1] if cand else None

    def capacity_truth(self, obj_id):
        obs = [o for o in self.capacity_obs.get(obj_id, []) if o["supersededBy"] is None]
        return obs[-1] if obs else None

    def append_status(self, node_id, status, event_date):
        self.status_events.append({"node": node_id, "status": status, "date": event_date,
                                   "tx": time.strftime("%Y-%m-%d")})
        o = self.objects.get(node_id)
        if o:
            o["status"] = status

    # ── Audit ──
    def append_audit(self, record: dict):
        self.audit_events.append(dict(record))

    def list_audit(self, limit: int = 100):
        return list(self.audit_events[-limit:])

    def count_objects_by_title(self, title):
        return sum(1 for o in self.objects.values() if o.get("title") == title)


# Backward-compat alias
KnowledgeStore = InMemoryKnowledgeStore
