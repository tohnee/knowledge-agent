"""CLOSED-LOOP TEST — proves the three layers form one working system.
Run:  python -m tests.test_closed_loop  (from wka-fused/)

Validates the SEAMS (not just the parts):
  · ingest writes the ontology ONLY through the Action engine (audited)
  · incremental: re-mentioned entities merge (not duplicate) — still via Action
  · controlled docs enqueue MarkExportControlled (compliance待审), not auto-marked
  · retrieval reads the SAME engine stores ingest filled
  · the AUTHORITATIVE OPA/Vault pass filters controlled content per role
  · the Action engine enforces permission + bitemporal write
"""
import sys
from common.models import Document, SourceTier
from api.system import System

P, F = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
res = []
def ck(name, cond, detail=""):
    res.append(cond); print(f"  {P if cond else F} {name}" + (f"  ({detail})" if detail else ""))

DOCS = [
    Document("d1", "foundry_Q4_earnings.pdf",
        "# 财务概况\n代工厂A 营收强劲。\n# 按制程营收\nN3 营收占比 26%，状态 HVM。N5 营收占比 22%。\n# 产能\nArizona Fab 月产能约 120000 片。N2 进入 risk production。",
        SourceTier.OFFICIAL),
    Document("d2", "analyst_report.pdf",
        "# 份额估计\n机构估计 N3 良率约 0.82。先进节点份额估计。",
        SourceTier.ANALYST),
    Document("d3", "equipment_techbrief.pdf",
        "# EUV 设备\nEUV 光刻机是先进逻辑制程关键瓶颈设备，受出口管制。N3 与 N5 依赖 EUV。",
        SourceTier.OFFICIAL, controlled=True),
    Document("d4", "news_capacity.pdf",
        "# 行业新闻\n代工厂A 宣布 N3 扩产，EUV 设备采购增加。",  # re-mentions N3/代工厂A/EUV → merge
        SourceTier.ANALYST),
]

sys_ = System()

print("\n=== SEAM 1: Ingest writes ontology ONLY via Action engine ===")
out = [sys_.ingest_doc(d) for d in DOCS]
by = {o["id"]: o for o in out}
ck("ingest routed through action_engine", all(o.get("via") == "action_engine" for o in out if o["status"] != "deduped"))
ck("d1 created objects via Action", by["d1"].get("objects_created", 0) > 0,
   f"created={by['d1'].get('objects_created')}")
ck("audit log recorded writes", len(sys_.actions.audit) > 0, f"{len(sys_.actions.audit)} audited actions")
ck("every audited write names a role", all("role" in a for a in sys_.actions.audit))

print("\n=== SEAM 2: Incremental — re-mentioned entities MERGE, not duplicate ===")
ck("d4 merged entities (N3/代工厂A/EUV already exist)", by["d4"].get("objects_merged", 0) > 0,
   f"merged={by['d4'].get('objects_merged')}, created={by['d4'].get('objects_created')}")
# count distinct N3 objects in the governed store — must be exactly 1
n3_count = sys_.store.count_objects_by_title("N3")
ck("exactly one N3 Object in governed store (no dup)", n3_count == 1, f"{n3_count} N3 objects")

print("\n=== SEAM 3: Controlled doc → MarkExportControlled待审 (not auto) ===")
ck("controlled d3 enqueued a control candidate", len(sys_.ingest.pending_controls) > 0,
   f"{len(sys_.ingest.pending_controls)} pending")
ck("control NOT auto-applied (needs compliance Action)",
   not any(a["action"] == "mark" for a in sys_.actions.audit))

print("\n=== SEAM 4: Retrieval reads the SAME stores ingest filled ===")
r = sys_.ask("N3 的状态是什么", role="analyst")
ck("ask returns grounded answer from shared stores", r["grounded"], r["answer"][:40])
ck("answer carries citations", isinstance(r["citations"], list))
ck("funnel ran (timing present)", bool(r.get("timing")))

print("\n=== SEAM 5: AUTHORITATIVE OPA/Vault security per role ===")
# EUV controlled content: compliance clear, analyst masked, viewer hidden/withheld
ra = sys_.ask("EUV 设备供应", role="analyst")
rc = sys_.ask("EUV 设备供应", role="compliance")
rv = sys_.ask("EUV 设备供应", role="viewer")
ctext = " ".join(c["text"] for c in rc["contexts"])
atext = " ".join(c["text"] for c in ra["contexts"])
vtext = " ".join(c["text"] for c in rv["contexts"])
ck("compliance sees controlled EUV content CLEAR", "出口管制" in ctext or "瓶颈" in ctext,
   f"{len(rc['contexts'])} ctx")
ck("analyst gets controlled content MASKED (not plaintext)",
   "受控字段" in atext and "出口管制" not in atext,
   f"masked={ra['masked_by_security']}")
ck("analyst.filtered flag is truthful", ra["filtered"] is True)
ck("viewer gets controlled content WITHHELD (hidden/pre-filtered)",
   "出口管制" not in vtext, f"viewer ctx={len(rv['contexts'])}")

print("\n=== SEAM 6: Action engine enforces permission + bitemporal ===")
# analyst cannot MarkExportControlled (compliance only)
denied = False
try:
    sys_.run_action("mark", {"entityId": "d3", "eccn": "X"}, role="analyst")
except Exception:
    denied = True
ck("analyst denied MarkExportControlled", denied)
# compliance can, but it's high-risk → sandbox first
sandbox = sys_.run_action("mark", {"entityId": "d3", "eccn": "X"}, role="compliance")
ck("compliance mark → sandbox preview first", sandbox["status"] == "pending_review")
committed = sys_.run_action("mark", {"entityId": "d3", "eccn": "X"}, role="compliance", confirmed=True)
ck("compliance mark commits after confirm", committed["status"] == "executed")

# bitemporal capacity via Action (ReviseCapacity append-only)
sys_.run_action("revise-capacity", {"fabId": "d1", "capacityWSPM": 78000, "asOf": "2023",
                                     "sourceTier": "analyst", "confidence": 0.78}, role="analyst")
sys_.run_action("revise-capacity", {"fabId": "d1", "capacityWSPM": 120000, "asOf": "2024",
                                     "sourceTier": "official", "confidence": 0.95}, role="analyst")
asof23 = sys_.store.capacity_asof("d1", 2023)
truth = sys_.store.capacity_truth("d1")
ck("as-of 2023 sees the 78K (decision-time) value", asof23 and asof23["capacityWSPM"] == 78000,
   str(asof23 and asof23["capacityWSPM"]))
ck("truth sees the latest 120K", truth and truth["capacityWSPM"] == 120000,
   str(truth and truth["capacityWSPM"]))

print("\n" + "=" * 54)
ok = sum(res)
print(f"  CLOSED-LOOP RESULT: {ok}/{len(res)} seam checks passed")
print("=" * 54)
sys.exit(0 if ok == len(res) else 1)
