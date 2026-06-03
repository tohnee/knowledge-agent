"""Verifier (self-critique) test — Claude Code judging DeepSeek/GLM output.
Fake judge client (no LLM server). Validates:
  · unsupported/hallucinated fact → confidence floored < 0.6 → dropped
  · weak fact → 0.6–0.85 → marked review_status=pending
  · object with wrong type/drop=true → removed
  · controlled doc → judge forced LOCAL (egress gate)
  · judge failure → conservative fallback (everything pending, not silently trusted)
  · end-to-end through AgentExtractor (extract → verify → assemble)

Run:  python -m tests.test_verifier
"""
import sys, json
from common.models import Document, SourceTier, Tier
from engine.ingest.extract.verifier import ExtractionVerifier
from engine.ingest.extract.agent_extractor import AgentExtractor
from engine.ingest.extract.llm_client import OpenAICompatClient, LLMEndpoint, EgressViolation

P, F = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
res = []
def ck(n, c, d=""):
    res.append(c); print(f"  {P if c else F} {n}" + (f"  ({d})" if d else ""))

LOCAL = {
    "deepseek-local": LLMEndpoint("deepseek-local", "http://vllm:8000/v1", "deepseek-v3", local=True),
    "glm-local": LLMEndpoint("glm-local", "http://ollama:11434/v1", "glm-4", local=True),
}
CLOUD = dict(LOCAL)
CLOUD["judge-cloud"] = LLMEndpoint("judge-cloud", "https://api.x.com/v1", "j", local=False)


class FakeJudge(OpenAICompatClient):
    """Judge that: drops a hallucinated fact, weakens another, keeps a supported one,
    and drops a wrong-typed object."""
    def __init__(self, eps, mode="normal"):
        super().__init__(eps); self.mode = mode; self.calls = []
    def chat(self, key, messages, *, controlled=False, **kw):
        ep = self.endpoints[key]
        if controlled and not ep.local:
            raise EgressViolation("judge controlled→cloud")
        self.calls.append(key)
        if self.mode == "fail":
            return "garbage not json"
        # verdict: N3.status supported(+0.05); N3.fake_metric unsupported(drop);
        #          BadEntity wrong type (drop object)
        return json.dumps({"objects": [
            {"object_id": "N3", "valid_type": True, "drop": False, "facts": [
                {"key": "status", "supported": True, "issue": "", "confidence_delta": 0.05},
                {"key": "fake_metric", "supported": False, "issue": "hallucinated", "confidence_delta": -0.5},
                {"key": "weak", "supported": True, "issue": "unit missing", "confidence_delta": -0.05}]},
            {"object_id": "BadEntity", "valid_type": False, "drop": True, "facts": []}]})


# extraction to be judged (as if from DeepSeek/GLM)
EXTRACTION = {"objects": [
    {"id": "N3", "type": "ProcessNode", "facts": [
        {"key": "status", "value": "HVM", "confidence": 0.82},
        {"key": "fake_metric", "value": "瞎编的", "confidence": 0.8},
        {"key": "weak", "value": "0.9", "confidence": 0.78}]},
    {"id": "BadEntity", "type": "Wrong", "facts": [{"key": "x", "value": "y", "confidence": 0.9}]},
], "links": [{"lt": "requiresEquipment", "from": "N3", "to": "EUV"}]}

CHAPTER = "# 节点\nN3 状态 HVM。weak 指标约 0.9。"

print("\n=== self-critique re-scoring ===")
v = ExtractionVerifier(llm=FakeJudge(LOCAL), orchestrator="direct")
out = v.verify_extraction(CHAPTER, EXTRACTION, controlled=False)
rev = out["_review"]
n3 = next((o for o in out["objects"] if o["id"] == "N3"), None)
keys = {f["key"]: f for f in (n3["facts"] if n3 else [])}
ck("verified flag set", rev.get("verified") is True)
ck("hallucinated fact dropped (<0.6)", "fake_metric" not in keys, f"kept={list(keys)}")
ck("supported fact survived + confidence raised", "status" in keys and keys["status"]["confidence"] > 0.82,
   keys.get("status", {}).get("confidence"))
ck("weak fact routed to review (0.6–0.85)", "weak" in keys and keys["weak"].get("review_status") == "pending",
   keys.get("weak", {}).get("confidence"))
ck("wrong-typed object dropped", all(o["id"] != "BadEntity" for o in out["objects"]))
ck("review summary counts present", rev["facts_dropped"] >= 1 and rev["objects_dropped"] >= 1,
   f"facts_dropped={rev['facts_dropped']} objs_dropped={rev['objects_dropped']}")

print("\n=== judge egress gate (controlled → local only) ===")
fc = FakeJudge(CLOUD)
vc = ExtractionVerifier(llm=fc, orchestrator="direct", judge_key="judge-cloud", local_judge_key="deepseek-local")
vc.verify_extraction(CHAPTER, EXTRACTION, controlled=True)
ck("controlled verification used LOCAL judge (not cloud)", all(k == "deepseek-local" for k in fc.calls),
   str(fc.calls))
# direct controlled→cloud must raise
raised = False
try:
    fc.chat("judge-cloud", [{"role": "user", "content": "x"}], controlled=True)
except EgressViolation:
    raised = True
ck("controlled→cloud judge raises EgressViolation", raised)

print("\n=== judge failure → conservative fallback ===")
vf = ExtractionVerifier(llm=FakeJudge(LOCAL, mode="fail"), orchestrator="direct")
outf = vf.verify_extraction(CHAPTER, EXTRACTION, controlled=False)
ck("judge failure → not silently trusted (all pending)", outf["_review"].get("all_pending") is True)
ck("fallback keeps objects but flags pending",
   all(all(f.get("review_status") == "pending" for f in o["facts"]) for o in outf["objects"]))

print("\n=== end-to-end through AgentExtractor (extract→verify→assemble) ===")
class FakeExtractLLM(OpenAICompatClient):
    def chat(self, key, messages, *, controlled=False, **kw):
        msg = messages[-1]["content"]
        if "质检裁判" in msg or "抽取结果 JSON" in msg:   # this is the JUDGE call
            return json.dumps({"objects": [{"object_id": "N3", "valid_type": True, "drop": False,
                "facts": [{"key": "status", "supported": True, "issue": "", "confidence_delta": 0.05}]}]})
        # this is the EXTRACT call
        return ('{"objects":[{"id":"N3","type":"ProcessNode","facts":['
                '{"key":"status","value":"HVM","sourceTier":"official","confidence":0.82,"asOf":"2024"}]}],'
                '"links":[]}')

ax = AgentExtractor(llm=FakeExtractLLM(LOCAL), semi_prompt_path="/nonexistent",
                    orchestrator="direct", verify=True)
o = ax.extract(Document("d1", "e.pdf", "# 节点\nN3 状态 HVM。", SourceTier.OFFICIAL), Tier.A)
ck("end-to-end produced entities after verify", len(o["entities"]) >= 1, f"{len(o['entities'])}")
ck("extractor exposes review summary", "facts_pending_review" in ax.review)

print("\n" + "=" * 50)
ok = sum(res)
print(f"  VERIFIER: {ok}/{len(res)} passed")
print("=" * 50)
sys.exit(0 if ok == len(res) else 1)
