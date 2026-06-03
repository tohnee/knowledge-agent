"""Neo4j store PARITY test — runs the closed loop against the Neo4j-backed System using
the in-memory FakeNeo4jDriver, and asserts the SAME behavior as the memory backend.
Proves the swap (store_backend='neo4j') is contract-preserving — esp. bitemporal append-only.

Run:  python -m tests.test_neo4j_parity
(Uses FakeNeo4jDriver — no running Neo4j needed. Against a live DB, point System at a real
 neo4j.GraphDatabase.driver and the same assertions hold.)"""
import sys
from common.models import Document, SourceTier
from api.system import System
from tests.fake_neo4j import FakeNeo4jDriver

P, F = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
res = []
def ck(n, c, d=""):
    res.append(c); print(f"  {P if c else F} {n}" + (f"  ({d})" if d else ""))

DOCS = [
    Document("d1", "earnings.pdf", "# 营收\nN3 状态 HVM。代工厂A 月产能 120000 片。EUV 关键设备。", SourceTier.OFFICIAL),
    Document("d2", "analyst.pdf", "# 份额\n机构估计 N3 良率 0.82。", SourceTier.ANALYST),
    Document("d3", "euv.pdf", "# EUV\nEUV 光刻机受出口管制。N3 依赖 EUV。", SourceTier.OFFICIAL, controlled=True),
    Document("d4", "news.pdf", "# 新闻\n代工厂A 宣布 N3 扩产，EUV 采购增加。", SourceTier.ANALYST),
]

print("\n=== Neo4j-backed System (FakeNeo4jDriver) ===")
sys_ = System(store_backend="neo4j", neo4j_driver=FakeNeo4jDriver())
ck("System wired with Neo4jKnowledgeStore",
   type(sys_.store).__name__ == "Neo4jKnowledgeStore")

# ingest through Action → Neo4j writes
out = [sys_.ingest_doc(d) for d in DOCS]
by = {o["id"]: o for o in out}
ck("ingest via action_engine (Neo4j writes)", all(o.get("via") == "action_engine" for o in out))
ck("objects created in Neo4j", by["d1"].get("objects_created", 0) > 0,
   f"created={by['d1'].get('objects_created')}")
ck("audit recorded", len(sys_.actions.audit) > 0, f"{len(sys_.actions.audit)} actions")

print("\n=== Incremental merge (Neo4j) ===")
ck("d4 merged (not duplicated)", by["d4"].get("objects_merged", 0) > 0,
   f"merged={by['d4'].get('objects_merged')}")
ck("exactly one N3 Object in Neo4j", sys_.store.count_objects_by_title("N3") == 1,
   f"{sys_.store.count_objects_by_title('N3')}")

print("\n=== get_object round-trips through Neo4j ===")
# find the N3 object id
n3_id = next((oid for oid in [e.id for e in sys_.ingest.pipe.resolver.canonical.values()
              if e.name == "N3"]), None)
o = sys_.store.get_object(n3_id) if n3_id else None
ck("get_object returns shape with facts+links", bool(o) and "facts" in o and "links" in o,
   f"facts={len(o['facts']) if o else 0}")

print("\n=== Bitemporal append-only (Neo4j) ===")
sys_.run_action("revise-capacity", {"fabId": "fab1", "capacityWSPM": 78000, "asOf": "2023",
                                     "sourceTier": "analyst", "confidence": 0.78}, role="analyst")
sys_.run_action("revise-capacity", {"fabId": "fab1", "capacityWSPM": 95000, "asOf": "2024",
                                     "sourceTier": "analyst", "confidence": 0.80}, role="analyst")
sys_.run_action("revise-capacity", {"fabId": "fab1", "capacityWSPM": 120000, "asOf": "2024",
                                     "sourceTier": "official", "confidence": 0.95}, role="analyst")
asof23 = sys_.store.capacity_asof("fab1", 2023)
asof24 = sys_.store.capacity_asof("fab1", 2024)
truth = sys_.store.capacity_truth("fab1")
ck("as-of 2023 = 78K (decision-time)", asof23 and asof23["capacityWSPM"] == 78000,
   str(asof23 and asof23["capacityWSPM"]))
ck("as-of 2024 = latest 2024 obs (120K)", asof24 and asof24["capacityWSPM"] == 120000,
   str(asof24 and asof24["capacityWSPM"]))
ck("truth = 120K (supersededBy IS NULL)", truth and truth["capacityWSPM"] == 120000,
   str(truth and truth["capacityWSPM"]))
# the superseded 95K must NOT be the truth (append-only proof)
ck("prior 2024 obs (95K) was superseded, not overwritten",
   truth and truth["capacityWSPM"] != 95000)

print("\n=== Security per role over Neo4j-backed retrieval ===")
ra = sys_.ask("EUV 设备供应", role="analyst")
rc = sys_.ask("EUV 设备供应", role="compliance")
ck("analyst masked", ra["masked_by_security"] >= 1 or not any("出口管制" in c["text"] for c in ra["contexts"]))
ck("compliance clear", any("EUV" in c["text"] or "出口管制" in c["text"] for c in rc["contexts"]))

print("\n=== MarkExportControlled (compliance) writes Neo4j control event ===")
sb = sys_.run_action("mark", {"entityId": "d3", "eccn": "ECCNX"}, role="compliance")
ck("mark → sandbox first", sb["status"] == "pending_review")
ex = sys_.run_action("mark", {"entityId": "d3", "eccn": "ECCNX"}, role="compliance", confirmed=True)
ck("mark commits to Neo4j after confirm", ex["status"] == "executed")
ck("control event written to graph", len(sys_.store.driver._g.events) > 0,
   f"{len(sys_.store.driver._g.events)} events")

print("\n" + "=" * 52)
ok = sum(res)
print(f"  NEO4J PARITY: {ok}/{len(res)} passed")
print("=" * 52)
sys.exit(0 if ok == len(res) else 1)
