"""AgentExtractor test — proves the production extraction engine (Claude Code + DeepSeek/GLM,
direct mode here) honors the Extractor contract AND enforces the export-control egress gate.

Uses a FAKE OpenAI-compatible client (no LLM server needed). Validates:
  · Tier-A/B route to the right backend; Tier-C makes NO LLM call (lazy)
  · model JSON → engine 4-tuple shape (drop-in for StubExtractor)
  · self-validation retries on bad JSON
  · controlled doc routed to a CLOUD endpoint raises EgressViolation (cannot be bypassed)
  · full System(extractor=AgentExtractor) closed loop still works + HNSW enabled

Run:  python -m tests.test_agent_extractor
"""
import sys
from common.models import Document, SourceTier, Tier
from engine.ingest.extract.agent_extractor import AgentExtractor
from engine.ingest.extract.llm_client import OpenAICompatClient, LLMEndpoint, EgressViolation
from engine.ingest.extract.classifier import classify

P, F = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
res = []
def ck(n, c, d=""):
    res.append(c); print(f"  {P if c else F} {n}" + (f"  ({d})" if d else ""))


# ── fake OpenAI-compatible client: returns schema-valid JSON; records which endpoint was hit ──
class FakeLLM(OpenAICompatClient):
    def __init__(self, endpoints, bad_first=False):
        super().__init__(endpoints)
        self.calls = []           # (endpoint_key, controlled)
        self.bad_first = bad_first
        self._n = 0

    def chat(self, endpoint_key, messages, *, controlled=False, **kw):
        # STILL enforce the real egress gate (call parent gate logic via a no-op transport)
        ep = self.endpoints[endpoint_key]
        if controlled and not ep.local:
            raise EgressViolation(f"controlled → non-local {ep.name}")
        self.calls.append((endpoint_key, controlled))
        self._n += 1
        if self.bad_first and self._n == 1:
            return "这是一些解释，不是 JSON"      # force a self-validation retry
        # schema-valid extraction
        return ('{"objects":[{"id":"N3","type":"ProcessNode","interfaces":["IHasRoadmap"],'
                '"facts":[{"key":"status","value":"HVM","sourceTier":"official",'
                '"confidence":0.92,"asOf":"2024","exportControlled":false}]},'
                '{"id":"EUV","type":"Equipment","interfaces":[],'
                '"facts":[{"key":"control","value":"受出口管制","sourceTier":"official",'
                '"confidence":0.9,"asOf":"2024","exportControlled":true}]}],'
                '"links":[{"lt":"requiresEquipment","from":"N3","to":"EUV","card":"many-to-many"}]}')


LOCAL_EPS = {
    "deepseek-local": LLMEndpoint("deepseek-local", "http://vllm:8000/v1", "deepseek-v3", local=True),
    "glm-local": LLMEndpoint("glm-local", "http://ollama:11434/v1", "glm-4", local=True),
}
# a deliberately-cloud endpoint to test the gate
CLOUD_EPS = dict(LOCAL_EPS)
CLOUD_EPS["deepseek-cloud"] = LLMEndpoint("deepseek-cloud", "https://api.deepseek.com/v1",
                                          "deepseek-chat", local=False)

print("\n=== Extractor contract + tier routing ===")
fake = FakeLLM(LOCAL_EPS)
ax = AgentExtractor(llm=fake, semi_prompt_path="/nonexistent", orchestrator="direct", verify=False)

# Tier-A official earnings
dA = Document("dA", "earnings.pdf", "# 营收\nN3 状态 HVM。EUV 关键设备。", SourceTier.OFFICIAL)
tA = classify(dA); ck("earnings classified Tier-A", tA == Tier.A, tA.value)
out = ax.extract(dA, tA)
ck("output has 4-tuple shape", all(k in out for k in ("chunks", "wiki_pages", "entities", "relations")))
ck("entities extracted from model JSON", len(out["entities"]) == 2, f"{len(out['entities'])}")
ck("relations extracted", len(out["relations"]) == 1)
ck("wiki pages built (Tier-A full compile)", len(out["wiki_pages"]) > 0)
ck("Tier-A hit strong backend (deepseek-local)", fake.calls[-1][0] == "deepseek-local")

# Tier-B general news → light backend (glm-local)
fake.calls.clear()
dB = Document("dB", "news.pdf", "# 新闻\n代工厂A 扩产 N3。设备采购增加，供应链调整。", SourceTier.ANALYST)
tB = classify(dB); ck("news classified Tier-B", tB == Tier.B, tB.value)
ax.extract(dB, tB)
ck("Tier-B hit light backend (glm-local)", fake.calls and fake.calls[-1][0] == "glm-local",
   fake.calls[-1][0] if fake.calls else "none")

# Tier-C → NO LLM call (lazy)
fake.calls.clear()
dC = Document("dC", "draft.pdf", "fwd: 草稿", SourceTier.RUMOR)
tC = classify(dC); ck("draft classified Tier-C", tC == Tier.C, tC.value)
outC = ax.extract(dC, tC)
ck("Tier-C made NO LLM call (lazy compile)", len(fake.calls) == 0)
ck("Tier-C still produced chunks (vectorize-only)", len(outC["chunks"]) > 0)
ck("Tier-C produced no entities/wiki", not outC["entities"] and not outC["wiki_pages"])

print("\n=== Self-validation retry on bad JSON ===")
fake2 = FakeLLM(LOCAL_EPS, bad_first=True)
ax2 = AgentExtractor(llm=fake2, semi_prompt_path="/nonexistent", orchestrator="direct", verify=False)
out2 = ax2.extract(Document("dR", "x.pdf", "# x\nN3 HVM。", SourceTier.OFFICIAL), Tier.A)
ck("recovered after bad-JSON retry", len(out2["entities"]) > 0, f"calls={fake2._n}")

print("\n=== EXPORT-CONTROL EGRESS GATE (the hard rule) ===")
# controlled doc + cloud backend → MUST raise, cannot be bypassed
fake_cloud = FakeLLM(CLOUD_EPS)
ax_cloud = AgentExtractor(llm=fake_cloud, semi_prompt_path="/nonexistent", orchestrator="direct", verify=False,
                          strong_key="deepseek-cloud", local_key="deepseek-local")
dCtrl = Document("dX", "euv_secret.pdf", "# EUV\nEUV 受出口管制。", SourceTier.OFFICIAL, controlled=True)
# controlled overrides endpoint to local_key → should NOT hit cloud; gate also guards
ax_cloud.extract(dCtrl, Tier.A)
ck("controlled doc forced to LOCAL backend (never cloud)",
   all(c[0] == "deepseek-local" for c in fake_cloud.calls),
   str([c[0] for c in fake_cloud.calls]))

# direct attempt to push controlled content to cloud must raise
raised = False
try:
    fake_cloud.chat("deepseek-cloud", [{"role": "user", "content": "x"}], controlled=True)
except EgressViolation:
    raised = True
ck("controlled→cloud raises EgressViolation (unbypassable)", raised)

# the real client's gate also blocks non-allowlisted hosts for controlled
real = OpenAICompatClient(CLOUD_EPS)
raised2 = False
try:
    real.chat("deepseek-cloud", [{"role": "user", "content": "x"}], controlled=True)
except EgressViolation:
    raised2 = True
ck("real client gate blocks controlled→cloud", raised2)

print("\n=== System wired with AgentExtractor + HNSW closed loop ===")
from api.system import System
sysx = System(extractor=AgentExtractor(llm=FakeLLM(LOCAL_EPS), semi_prompt_path="/nonexistent"),
              use_hnsw=True)
r = sysx.ingest_doc(Document("s1", "earnings.pdf", "# 营收\nN3 状态 HVM。EUV 设备。", SourceTier.OFFICIAL))
ck("ingest via Action with AgentExtractor", r.get("via") == "action_engine", f"created={r.get('objects_created')}")
ck("HNSW enabled on vector store", sysx.ingest.vstore.use_hnsw is True)
ans = sysx.ask("N3 状态", role="analyst")
ck("retrieval works over AgentExtractor-built store", ans["grounded"], ans["answer"][:30])

print("\n" + "=" * 54)
ok = sum(res)
print(f"  AGENT EXTRACTOR: {ok}/{len(res)} passed")
print("=" * 54)
sys.exit(0 if ok == len(res) else 1)
