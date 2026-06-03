"""A tiny FAKE Neo4j driver for unit tests — interprets the exact Cypher patterns the
Neo4jKnowledgeStore issues, against an in-memory graph. It is NOT a general Cypher engine;
it recognizes this store's queries by signature and reproduces their semantics, so we can
verify the store honors the KnowledgeStoreBase contract (esp. bitemporal append-only)
WITHOUT a running Neo4j. Real correctness is validated against a live DB in CI."""
from __future__ import annotations
import re, time


class _Graph:
    def __init__(self):
        self.objects = {}        # id → props
        self.facts = {}          # obj_id → {key: fact}
        self.wiki = {}           # id → props
        self.links = []          # {src,dst,lt,docId}
        self.capacity = {}       # obj_id → [rel props]
        self.status = []         # {node,status,...}
        self.events = []         # control/status events
        self.audit = []          # AuditEvent nodes


class _Session:
    def __init__(self, g): self.g = g
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def run(self, cypher, **p):
        c = " ".join(cypher.split())  # normalize whitespace
        g = self.g

        # ── apoc probe ──
        if "apoc.version()" in c:
            raise Exception("no apoc in fake")

        # ── put_object: MERGE Object SET ... ──
        if c.startswith("MERGE (o:Object {id:$id}) SET o.objectType"):
            o = g.objects.setdefault(p["id"], {"id": p["id"]})
            o.update({"objectType": p["type"], "title": p["title"], "confidence": p["conf"],
                      "controlled": p["ctrl"], "aliases": p["aliases"], "doc_ids": p["docids"],
                      "sourceTier": p["tier"]})
            return _Res([])

        # ── put_object facts ──
        if "MERGE (o)-[:HAS_FACT]->(fct:Fact {key:$k})" in c:
            g.facts.setdefault(p["id"], {})[p["k"]] = {
                "key": p["k"], "value": p["v"], "controlled": p["c"],
                "sourceTier": p["t"], "confidence": p["conf"], "asOf": p["asof"]}
            return _Res([])

        # ── get_object: main ──
        if c.startswith("MATCH (o:Object {id:$id}) OPTIONAL MATCH (o)-[r]->(t:Object)"):
            o = g.objects.get(p["id"])
            if not o:
                return _Res([{"o": None, "links": []}])
            links = [{"lt": l["lt"], "to": l["dst"]} for l in g.links if l["src"] == p["id"]]
            return _Res([{"o": dict(o), "links": links or [{"lt": None, "to": None}]}])

        # ── get_object: facts ──
        if "MATCH (o:Object {id:$id})-[:HAS_FACT]->(f:Fact)" in c and "RETURN f.key" in c:
            return _Res([dict(v) for v in g.facts.get(p["id"], {}).values()])

        # ── merge_object (non-apoc branch) ──
        if "WITH o, [x IN coalesce(o.aliases,[])" in c:
            o = g.objects.get(p["id"])
            if o:
                o["aliases"] = list(dict.fromkeys((o.get("aliases") or []) + p["aliases"]))
                o["doc_ids"] = list(dict.fromkeys((o.get("doc_ids") or []) + p["docids"]))
            return _Res([])

        # ── set_controlled ──
        if c.startswith("MATCH (o:Object {id:$id}) SET o.controlled=$v, o.eccn=$e"):
            o = g.objects.get(p["id"])
            if o: o["controlled"] = p["v"]; o["eccn"] = p["e"]
            for f in g.facts.get(p["id"], {}).values(): f["controlled"] = p["v"]
            return _Res([])
        if "MATCH (w:WikiPage) WHERE w.docId=$id OR w.id=$id SET w.controlled" in c:
            for w in g.wiki.values():
                if w.get("docId") == p["id"] or w.get("id") == p["id"]: w["controlled"] = p["v"]
            return _Res([])
        if "CREATE (o)-[:HAS_EVENT]->(:ExportControlEvent" in c:
            g.events.append({"obj": p["id"], "eccn": p["e"], "newStatus": p["v"], "at": time.time()})
            return _Res([])

        # ── bump_confidence ──
        if "SET o.confidence = CASE WHEN coalesce(o.confidence,0.78)+$d" in c:
            o = g.objects.get(p["id"])
            if o: o["confidence"] = min(1.0, o.get("confidence", 0.78) + p["d"])
            return _Res([])

        # ── put_link ──
        if "MERGE (a)-[r:LINK {lt:$lt}]->(b)" in c:
            g.objects.setdefault(p["src"], {"id": p["src"]})
            g.objects.setdefault(p["dst"], {"id": p["dst"]})
            if not any(l["src"] == p["src"] and l["dst"] == p["dst"] and l["lt"] == p["lt"] for l in g.links):
                g.links.append({"src": p["src"], "dst": p["dst"], "lt": p["lt"], "docId": p["doc"]})
            return _Res([])

        # ── put_wiki ──
        if c.startswith("MERGE (w:WikiPage {id:$id})"):
            w = g.wiki.setdefault(p["id"], {"id": p["id"], "version": 0})
            w.update({"docId": p["docid"], "title": p["title"], "summary": p["summary"],
                      "body": p["body"], "section": p["section"], "controlled": p["ctrl"],
                      "entities": p["entities"], "version": w.get("version", 0) + 1})
            return _Res([])
        if c.startswith("MATCH (w:WikiPage {id:$id}) RETURN w"):
            w = g.wiki.get(p["id"])
            return _Res([{"w": dict(w) if w else None}])

        # ── append_capacity: supersede prior ──
        if "WHERE c.validTime = $vt AND c.supersededBy IS NULL SET c.supersededBy" in c:
            for r in g.capacity.get(p["id"], []):
                if r["validTime"] == p["vt"] and r["supersededBy"] is None:
                    r["supersededBy"] = p["newid"]
            return _Res([])
        # ── append_capacity: create new obs ──
        if "CREATE (o)-[:HAS_CAPACITY {" in c:
            g.capacity.setdefault(p["id"], []).append({
                "capacityWSPM": p["w"], "validTime": p["vt"],
                "transactionTime": p["tx"], "sourceTier": p["tier"],
                "confidence": p["conf"], "supersededBy": None})
            return _Res([])

        # ── capacity_asof ──
        if "WHERE toInteger(left(c.validTime,4)) <= $y" in c:
            obs = [r for r in g.capacity.get(p["id"], []) if int(str(r["validTime"])[:4]) <= p["y"]]
            obs.sort(key=lambda r: (r["validTime"], r["transactionTime"]))
            return _Res([_cap_row(obs[-1])] if obs else [])
        # ── capacity_truth ──
        if "WHERE c.supersededBy IS NULL RETURN c.capacityWSPM" in c:
            obs = [r for r in g.capacity.get(p["id"], []) if r["supersededBy"] is None]
            obs.sort(key=lambda r: r["validTime"])
            return _Res([_cap_row(obs[-1])] if obs else [])

        # ── append_status ──
        if "CREATE (o)-[:STATUS_CHANGE {" in c:
            g.objects.setdefault(p["id"], {"id": p["id"]})["status"] = p["s"]
            g.status.append({"node": p["id"], "status": p["s"], "eventDate": p["d"]})
            return _Res([])


        # ── append_audit / list_audit ──
        if c.startswith("CREATE (:AuditEvent"):
            g.audit.append({
                "id": p["id"], "schemaVersion": p["schema"], "action": p["action"],
                "role": p["role"], "actor": p["actor"], "at": p["at"],
                "paramsHash": p["paramsHash"], "result": p["result"],
                "decision": p["decision"], "params": p["params"],
            })
            return _Res([])
        if "MATCH (a:AuditEvent)" in c and "RETURN a AS a" in c:
            rows = sorted(g.audit, key=lambda r: r.get("at", 0), reverse=True)[:p.get("limit", 100)]
            return _Res([{"a": dict(r)} for r in rows])

        # ── count_objects_by_title ──
        if "MATCH (o:Object {title:$t}) RETURN count(o)" in c:
            n = sum(1 for o in g.objects.values() if o.get("title") == p["t"])
            return _Res([{"n": n}])

        # schema / unknown → no-op
        return _Res([])


def _cap_row(r):
    return {"capacityWSPM": r["capacityWSPM"], "validTime": r["validTime"],
            "sourceTier": r["sourceTier"], "confidence": r["confidence"],
            "transactionTime": r["transactionTime"], "supersededBy": r["supersededBy"]}


class _Res:
    def __init__(self, rows): self._rows = rows
    def __iter__(self): return iter([_Rec(r) for r in self._rows])


class _Rec:
    def __init__(self, d): self._d = d
    def data(self): return self._d


class FakeNeo4jDriver:
    def __init__(self): self._g = _Graph()
    def session(self, database=None): return _Session(self._g)
    def close(self): pass
