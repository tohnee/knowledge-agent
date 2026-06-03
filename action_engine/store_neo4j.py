"""WKA business layer · Neo4j KnowledgeStore (production).
Same contract as InMemoryKnowledgeStore; backs the Action engine.

Graph model (design-doc §6 / §8.2):
  (:Object {id, objectType, title, confidence, controlled, eccn, status, ...})
  (:Object)-[:CO_OCCURS|...]->(:Object)                       links (typed by lt)
  (:Object)-[:HAS_CAPACITY {capacityWSPM, validTime, transactionTime,
             sourceTier, confidence, supersededBy}]->(:Observation)   ← bitemporal, append-only
  (:Object)-[:STATUS_CHANGE {status, eventDate, transactionTime}]->(:Event)
  (:WikiPage {id, docId, title, summary, body, controlled})   parent spans

Bitemporal rule preserved exactly: observations are APPENDED; the prior latest obs for
the same validTime gets supersededBy set; as-of = latest validTime<=year; truth =
latest supersededBy IS NULL.

Driver is injected (so it's unit-testable with a fake). In prod pass a neo4j.GraphDatabase
driver. All writes assume they're called *inside* the Action engine's logical transaction;
each method opens a session and runs an idempotent MERGE/CREATE."""
from __future__ import annotations
import json, os, time
from action_engine.store_base import KnowledgeStoreBase


class Neo4jKnowledgeStore(KnowledgeStoreBase):
    def __init__(self, driver=None, database: str = "neo4j"):
        if driver is None:
            from neo4j import GraphDatabase                       # imported lazily
            driver = GraphDatabase.driver(
                os.getenv("NEO4J_URI", "bolt://wka-neo4j:7687"),
                auth=("neo4j", os.getenv("NEO4J_PASSWORD", "neo4j")))
        self.driver = driver
        self.database = database

    def _run(self, cypher: str, **params):
        with self.driver.session(database=self.database) as s:
            return [r.data() for r in s.run(cypher, **params)]

    # ── Objects ──
    def get_object(self, oid):
        rows = self._run("""
            MATCH (o:Object {id:$id})
            OPTIONAL MATCH (o)-[r]->(t:Object)
            RETURN o AS o, collect({lt:type(r), to:t.id}) AS links
        """, id=oid)
        if not rows or rows[0]["o"] is None:
            return None
        o = dict(rows[0]["o"])
        # facts are stored as a JSON-ish list on the node; Neo4j can't hold nested dicts,
        # so facts live in a co-located :Fact subgraph — fetch them
        facts = self._run("""
            MATCH (o:Object {id:$id})-[:HAS_FACT]->(f:Fact)
            RETURN f.key AS key, f.value AS value, f.controlled AS controlled,
                   f.sourceTier AS sourceTier, f.confidence AS confidence, f.asOf AS asOf
        """, id=oid)
        o["facts"] = [dict(f) for f in facts]
        o["links"] = [l for l in rows[0]["links"] if l["to"] is not None]
        return o

    def put_object(self, obj: dict):
        self._run("""
            MERGE (o:Object {id:$id})
            SET o.objectType=$type, o.title=$title, o.confidence=$conf,
                o.controlled=$ctrl, o.aliases=$aliases, o.doc_ids=$docids,
                o.sourceTier=$tier, o.updatedAt=datetime()
        """, id=obj["id"], type=obj.get("objectType", ""), title=obj.get("title", ""),
            conf=float(obj.get("confidence", 0.78)), ctrl=bool(obj.get("controlled", False)),
            aliases=obj.get("aliases", []), docids=obj.get("doc_ids", []),
            tier=obj.get("sourceTier", "analyst"))
        # facts → :Fact nodes (Neo4j can't store nested dicts on a property)
        for f in obj.get("facts", []):
            self._run("""
                MATCH (o:Object {id:$id})
                MERGE (o)-[:HAS_FACT]->(fct:Fact {key:$k})
                SET fct.value=$v, fct.controlled=$c, fct.sourceTier=$t,
                    fct.confidence=$conf, fct.asOf=$asof
            """, id=obj["id"], k=f["key"], v=str(f.get("value", "")),
                c=bool(f.get("controlled", False)), t=f.get("sourceTier", "analyst"),
                conf=float(f.get("confidence", 0.78)), asof=str(f.get("asOf", "")))

    def merge_object(self, existing: dict, incoming: dict):
        # incremental: union aliases + doc_ids (idempotent SET with list concat + dedup in Cypher)
        self._run("""
            MATCH (o:Object {id:$id})
            SET o.aliases = apoc.coll.toSet(coalesce(o.aliases,[]) + $aliases),
                o.doc_ids = apoc.coll.toSet(coalesce(o.doc_ids,[]) + $docids)
        """, id=existing["id"], aliases=incoming.get("aliases", []),
            docids=incoming.get("doc_ids", [])) if self._has_apoc() else \
        self._run("""
            MATCH (o:Object {id:$id})
            WITH o, [x IN coalesce(o.aliases,[]) + $aliases | x] AS al,
                    [x IN coalesce(o.doc_ids,[]) + $docids | x] AS dd
            SET o.aliases = al, o.doc_ids = dd
        """, id=existing["id"], aliases=incoming.get("aliases", []),
            docids=incoming.get("doc_ids", []))

    def set_controlled(self, oid, val, eccn=""):
        self._run("""
            MATCH (o:Object {id:$id}) SET o.controlled=$v, o.eccn=$e
            WITH o MATCH (o)-[:HAS_FACT]->(f:Fact) SET f.controlled=$v
        """, id=oid, v=bool(val), e=eccn)
        self._run("MATCH (w:WikiPage) WHERE w.docId=$id OR w.id=$id SET w.controlled=$v",
                  id=oid, v=bool(val))
        # write the audit Event for the control mark (design-doc §9.2)
        self._run("""
            MATCH (o:Object {id:$id})
            CREATE (o)-[:HAS_EVENT]->(:ExportControlEvent {
                markedAt:datetime(), eccn:$e, newStatus:$v})
        """, id=oid, e=eccn, v=bool(val))

    def bump_confidence(self, oid, delta):
        self._run("""
            MATCH (o:Object {id:$id})
            SET o.confidence = CASE WHEN coalesce(o.confidence,0.78)+$d > 1.0
                                    THEN 1.0 ELSE coalesce(o.confidence,0.78)+$d END
        """, id=oid, d=float(delta))

    # ── Links ──
    def put_link(self, link: dict):
        # link type is dynamic → use a generic :LINK with lt property (safe, no Cypher injection)
        self._run("""
            MERGE (a:Object {id:$src})
            MERGE (b:Object {id:$dst})
            MERGE (a)-[r:LINK {lt:$lt}]->(b)
            SET r.docId=$doc, r.createdAt=datetime()
        """, src=link["src"], dst=link["dst"], lt=link.get("lt", "related"),
            doc=link.get("doc_id", ""))

    # ── Wiki pages ──
    def put_wiki(self, doc: dict):
        self._run("""
            MERGE (w:WikiPage {id:$id})
            SET w.docId=$docid, w.title=$title, w.summary=$summary, w.body=$body,
                w.section=$section, w.controlled=$ctrl, w.entities=$entities,
                w.version=coalesce(w.version,0)+1, w.updatedAt=datetime()
        """, id=doc["_id"], docid=doc.get("doc_id", ""), title=doc.get("title", ""),
            summary=doc.get("summary", ""), body=doc.get("body", ""),
            section=doc.get("section", ""), ctrl=bool(doc.get("controlled", False)),
            entities=doc.get("entities", []))

    def get_wiki(self, pid):
        rows = self._run("MATCH (w:WikiPage {id:$id}) RETURN w AS w", id=pid)
        if not rows or rows[0]["w"] is None:
            return None
        w = dict(rows[0]["w"])
        return {"_id": w.get("id"), "doc_id": w.get("docId"), "title": w.get("title"),
                "summary": w.get("summary"), "body": w.get("body"),
                "section": w.get("section"), "controlled": w.get("controlled"),
                "entities": w.get("entities", [])}

    # ── Bitemporal capacity (append-only, supersededBy) ──
    def append_capacity(self, obj_id, wspm, valid_time, source_tier, confidence):
        # 1) supersede prior latest obs for the SAME validTime
        self._run("""
            MATCH (o:Object {id:$id})-[c:HAS_CAPACITY]->()
            WHERE c.validTime = $vt AND c.supersededBy IS NULL
            SET c.supersededBy = $newid
        """, id=obj_id, vt=valid_time, newid=f"obs_{int(time.time()*1000)}")
        # 2) append the new observation (never overwrite)
        self._run("""
            MERGE (o:Object {id:$id})
            CREATE (o)-[:HAS_CAPACITY {
                capacityWSPM:$w, validTime:$vt, transactionTime:date($tx),
                sourceTier:$tier, confidence:$conf, supersededBy:null
            }]->(:Observation {id:$obsid})
        """, id=obj_id, w=int(wspm), vt=valid_time, tx=time.strftime("%Y-%m-%d"),
            tier=source_tier, conf=float(confidence), obsid=f"obs_{int(time.time()*1000)}")

    def capacity_asof(self, obj_id, year: int):
        rows = self._run("""
            MATCH (o:Object {id:$id})-[c:HAS_CAPACITY]->()
            WHERE toInteger(left(c.validTime,4)) <= $y
            RETURN c.capacityWSPM AS capacityWSPM, c.validTime AS validTime,
                   c.sourceTier AS sourceTier, c.confidence AS confidence,
                   toString(c.transactionTime) AS transactionTime, c.supersededBy AS supersededBy
            ORDER BY c.validTime DESC, c.transactionTime DESC LIMIT 1
        """, id=obj_id, y=year)
        return rows[0] if rows else None

    def capacity_truth(self, obj_id):
        rows = self._run("""
            MATCH (o:Object {id:$id})-[c:HAS_CAPACITY]->()
            WHERE c.supersededBy IS NULL
            RETURN c.capacityWSPM AS capacityWSPM, c.validTime AS validTime,
                   c.sourceTier AS sourceTier, c.confidence AS confidence,
                   toString(c.transactionTime) AS transactionTime, c.supersededBy AS supersededBy
            ORDER BY c.validTime DESC LIMIT 1
        """, id=obj_id)
        return rows[0] if rows else None

    def append_status(self, node_id, status, event_date):
        self._run("""
            MERGE (o:Object {id:$id})
            SET o.status=$s
            CREATE (o)-[:STATUS_CHANGE {status:$s, eventDate:$d,
                transactionTime:date($tx)}]->(:Event {type:'StatusChange'})
        """, id=node_id, s=status, d=event_date, tx=time.strftime("%Y-%m-%d"))

    # ── Audit ──
    def append_audit(self, record: dict):
        self._run("""
            CREATE (:AuditEvent {
                id:$id, schemaVersion:$schema, action:$action, role:$role, actor:$actor,
                at:$at, paramsHash:$paramsHash, result:$result, decision:$decision, params:$params
            })
        """, id=record["id"], schema=record.get("schemaVersion", 1),
            action=record.get("action", ""), role=record.get("role", ""),
            actor=record.get("actor", ""), at=float(record.get("at", 0.0)),
            paramsHash=record.get("paramsHash", ""),
            result=json.dumps(record.get("result", {}), ensure_ascii=False, sort_keys=True),
            decision=record.get("decision", ""),
            params=json.dumps(record.get("params", {}), ensure_ascii=False, sort_keys=True))

    def list_audit(self, limit: int = 100):
        rows = self._run("""
            MATCH (a:AuditEvent)
            RETURN a AS a
            ORDER BY a.at DESC LIMIT $limit
        """, limit=int(limit))
        return [dict(r["a"]) for r in rows if r.get("a") is not None]

    def count_objects_by_title(self, title):
        rows = self._run("MATCH (o:Object {title:$t}) RETURN count(o) AS n", t=title)
        return rows[0]["n"] if rows else 0

    # ── helpers ──
    def _has_apoc(self):
        if getattr(self, "_apoc", None) is None:
            try:
                self._run("RETURN apoc.version()")
                self._apoc = True
            except Exception:
                self._apoc = False
        return self._apoc

    def init_schema(self):
        """Idempotent constraints + indexes (run once on deploy)."""
        for stmt in [
            "CREATE CONSTRAINT obj_id IF NOT EXISTS FOR (o:Object) REQUIRE o.id IS UNIQUE",
            "CREATE CONSTRAINT wiki_id IF NOT EXISTS FOR (w:WikiPage) REQUIRE w.id IS UNIQUE",
            "CREATE INDEX obj_type IF NOT EXISTS FOR (o:Object) ON (o.objectType)",
            "CREATE INDEX cap_valid IF NOT EXISTS FOR ()-[c:HAS_CAPACITY]-() ON (c.validTime)",
            "CREATE INDEX cap_tx IF NOT EXISTS FOR ()-[c:HAS_CAPACITY]-() ON (c.transactionTime)",
            "CREATE INDEX audit_at IF NOT EXISTS FOR (a:AuditEvent) ON (a.at)",
        ]:
            try: self._run(stmt)
            except Exception: pass
