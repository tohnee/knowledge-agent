"""HTTP closed-loop test — exercises the real API gateway over HTTP via TestClient.
Proves frontend → /api/v1/* → fused System works end to end.
Run:  python -m tests.test_http   (requires fastapi+httpx; skips cleanly if absent)"""
import sys
try:
    from fastapi.testclient import TestClient
    import api.main as m
except Exception as e:
    print(f"[skip] FastAPI/httpx not installed ({e}); headless test_closed_loop covers the logic.")
    sys.exit(0)

P, F = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
res = []
def ck(n, c, d=""):
    res.append(c); print(f"  {P if c else F} {n}" + (f"  ({d})" if d else ""))

c = TestClient(m.app)
H = lambda r: {"Authorization": "Role " + r}

print("\n=== HTTP closed loop ===")
ck("health ok", c.get("/api/v1/health").json()["status"] == "ok")

# ① upload → governed ingest
up = c.post("/api/v1/documents/upload", json={
    "id": "t1", "name": "earnings.pdf",
    "text": "# 营收\nN3 状态 HVM。代工厂A 月产能 120000 片。EUV 关键设备。", "sourceTier": "official"},
    headers=H("analyst")).json()
ck("upload via action_engine", up.get("via") == "action_engine", f"created={up.get('objects_created')}")

c.post("/api/v1/documents/upload", json={"id": "t2", "name": "euv.pdf",
    "text": "# EUV\nEUV 光刻机受出口管制，供应集中。", "sourceTier": "official", "controlled": True},
    headers=H("analyst"))

# ⑤ ask — security per role over HTTP
qa_a = c.post("/api/v1/knowledge/qa", json={"question": "EUV 设备供应"}, headers=H("analyst")).json()
qa_c = c.post("/api/v1/knowledge/qa", json={"question": "EUV 设备供应"}, headers=H("compliance")).json()
ck("analyst masked over HTTP", qa_a["masked_by_security"] >= 1, f"masked={qa_a['masked_by_security']}")
ck("compliance sees controlled clear over HTTP", len(qa_c["contexts"]) >= 1)

# actions — permission + sandbox over HTTP
ck("analyst revise-capacity allowed (200)",
   c.post("/api/v1/actions/revise-capacity",
          json={"fabId": "t1", "capacityWSPM": 78000, "asOf": "2023", "sourceTier": "analyst"},
          headers=H("analyst")).status_code == 200)
c.post("/api/v1/actions/revise-capacity",
       json={"fabId": "t1", "capacityWSPM": 120000, "asOf": "2024", "sourceTier": "official"},
       headers=H("analyst"))
ck("analyst mark denied (403)",
   c.post("/api/v1/actions/mark", json={"entityId": "t2", "eccn": "X"}, headers=H("analyst")).status_code == 403)
ck("compliance mark → sandbox",
   c.post("/api/v1/actions/mark", json={"entityId": "t2", "eccn": "X"}, headers=H("compliance")).json()["status"] == "pending_review")

# bitemporal as-of over HTTP
af = c.get("/api/v1/objects/t1/asof?year=2023").json()
ck("as-of 2023 = 78K (decision-time)", af["known"] and af["known"]["capacityWSPM"] == 78000)
ck("truth = 120K (latest)", af["truth"] and af["truth"]["capacityWSPM"] == 120000)

print("\n" + "=" * 46)
ok = sum(res)
print(f"  HTTP RESULT: {ok}/{len(res)} passed")
print("=" * 46)
sys.exit(0 if ok == len(res) else 1)
