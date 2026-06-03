"""WKA · Extraction Verifier — the SECOND quality gate (self-critique loop).

Pattern: the cheap/local extractor (DeepSeek/GLM) does the first pass; a stronger judge
(Claude Code, or any judge model) reviews EACH extracted object against the source chapter
and returns verdicts that RE-SCORE confidence. This catches hallucinated facts, wrong units,
marketingNode↔physicalGateLength confusion, mis-typed entities, unsupported links.

Maps to the scaling plan §1.4 quality gate + §5.1 Phase-3 校验, and to the design doc's
confidence tiers: verdict adjusts confidence → ≥0.85 keep / 0.6–0.85 待审 / <0.6 丢弃.

Egress rule still holds: verifying a CONTROLLED document must use a LOCAL judge — the gate
is enforced via the same OpenAICompatClient (or Claude Code in local-only mode)."""
from __future__ import annotations
import json, re, os, subprocess
from dataclasses import dataclass, field
from engine.ingest.extract.llm_client import OpenAICompatClient, EgressViolation


# confidence bands (design doc §5.1)
KEEP_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.60


@dataclass
class FactVerdict:
    key: str
    supported: bool                 # is the fact grounded in the source chapter?
    issue: str = ""                 # e.g. "unit missing", "marketingNode≠gateLength", "hallucinated"
    confidence_delta: float = 0.0   # how much to adjust (-/+)


@dataclass
class ObjectVerdict:
    object_id: str
    valid_type: bool = True
    facts: list = field(default_factory=list)        # [FactVerdict]
    drop: bool = False                                # type wrong / entity hallucinated → drop


VERIFY_PROMPT = """你是抽取质检裁判（self-critique）。下面是【原始章节】和【抽取结果 JSON】。
请逐条核对抽取结果是否真实出自章节、单位是否齐全、类型是否正确、是否混淆 marketingNode 与物理参数、链接是否有依据。
只输出 JSON（无解释、无围栏）：
{"objects":[{"object_id","valid_type":bool,"drop":bool,
  "facts":[{"key","supported":bool,"issue":"","confidence_delta":number}]}]}
其中 confidence_delta ∈ [-0.5, +0.1]：无依据/幻觉给负值，证据充分可小幅加分。
【原始章节】
%s
【抽取结果 JSON】
%s
"""


class ExtractionVerifier:
    def __init__(self, llm: OpenAICompatClient | None = None,
                 orchestrator: str = "direct",          # "claude_code" | "direct"
                 judge_key: str = "deepseek-local",      # judge model (Claude Code or strong local)
                 local_judge_key: str = "deepseek-local",
                 enabled: bool = True):
        self.llm = llm
        self.orchestrator = orchestrator
        self.judge_key = judge_key
        self.local_judge_key = local_judge_key
        self.enabled = enabled

    def verify_extraction(self, chapter: str, extraction: dict, controlled: bool = False) -> dict:
        """Re-score `extraction` ({objects, links}) against `chapter`.
        Returns a NEW extraction dict with adjusted confidences, dropped objects/facts,
        plus a `_review` summary. Pure w.r.t. inputs (doesn't mutate caller's dict)."""
        if not self.enabled or not extraction.get("objects"):
            return {**extraction, "_review": {"verified": False, "reason": "disabled/empty"}}

        # controlled → force local judge (egress gate)
        judge = self.local_judge_key if controlled else self.judge_key
        try:
            raw = (self._judge_claude_code(chapter, extraction, controlled)
                   if self.orchestrator == "claude_code"
                   else self._judge_direct(chapter, extraction, judge, controlled))
            verdicts = self._parse(raw)
        except EgressViolation:
            raise
        except Exception as e:
            # judge failed → conservative: mark everything for review (don't silently trust)
            return self._fallback_review(extraction, reason=str(e))

        return self._apply(extraction, verdicts)

    # ── judge transports ──
    def _judge_direct(self, chapter, extraction, judge_key, controlled):
        prompt = VERIFY_PROMPT % (chapter[:6000], json.dumps(extraction, ensure_ascii=False)[:6000])
        return self.llm.chat(judge_key,
                             [{"role": "system", "content": "你是严格的抽取质检裁判。"},
                              {"role": "user", "content": prompt}],
                             controlled=controlled, temperature=0.0)

    def _judge_claude_code(self, chapter, extraction, controlled):
        if controlled and os.getenv("CLAUDE_CODE_LOCAL_ONLY") != "1":
            raise EgressViolation("Claude Code judge must be local-only for controlled docs.")
        prompt = VERIFY_PROMPT % (chapter[:6000], json.dumps(extraction, ensure_ascii=False)[:6000])
        out = subprocess.run(["claude", "-p", "--output-format", "json", prompt],
                             capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            raise RuntimeError(f"claude judge failed: {out.stderr[:160]}")
        return json.loads(out.stdout).get("result", out.stdout)

    # ── parse judge JSON → verdicts ──
    def _parse(self, raw: str) -> dict:
        text = raw.strip().replace("```json", "").replace("```", "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError("judge returned no JSON")
        data = json.loads(m.group(0))
        out = {}
        for ov in data.get("objects", []):
            oid = ov.get("object_id")
            if oid is None:
                continue
            out[oid] = ObjectVerdict(
                object_id=oid, valid_type=ov.get("valid_type", True), drop=ov.get("drop", False),
                facts=[FactVerdict(key=f.get("key", ""), supported=f.get("supported", True),
                                   issue=f.get("issue", ""),
                                   confidence_delta=float(f.get("confidence_delta", 0.0)))
                       for f in ov.get("facts", [])])
        return out

    # ── apply verdicts: adjust confidence, drop / route-to-review ──
    def _apply(self, extraction: dict, verdicts: dict) -> dict:
        kept_objs, review_count, dropped_facts, dropped_objs = [], 0, 0, 0
        for o in extraction["objects"]:
            v = verdicts.get(o.get("id") or o.get("title"))
            if v and (v.drop or not v.valid_type):
                dropped_objs += 1
                continue
            fact_verdicts = {fv.key: fv for fv in (v.facts if v else [])}
            new_facts = []
            for f in o.get("facts", []):
                fv = fact_verdicts.get(f.get("key"))
                conf = float(f.get("confidence", 0.78))
                if fv:
                    conf = max(0.0, min(1.0, conf + fv.confidence_delta))
                    if not fv.supported:
                        conf = min(conf, 0.55)        # unsupported → below review floor
                    f = {**f, "confidence": round(conf, 3), "verify_issue": fv.issue}
                # banding
                if conf < REVIEW_THRESHOLD:
                    dropped_facts += 1
                    continue                          # <0.6 dropped
                if conf < KEEP_THRESHOLD:
                    f["review_status"] = "pending"     # 0.6–0.85 待审
                    review_count += 1
                new_facts.append(f)
            o = {**o, "facts": new_facts}
            if new_facts:                              # object survives if any fact kept
                kept_objs.append(o)
            else:
                dropped_objs += 1
        return {"objects": kept_objs, "links": extraction.get("links", []),
                "_review": {"verified": True, "objects_in": len(extraction["objects"]),
                            "objects_kept": len(kept_objs), "objects_dropped": dropped_objs,
                            "facts_dropped": dropped_facts, "facts_pending_review": review_count}}

    def _fallback_review(self, extraction, reason):
        objs = []
        for o in extraction["objects"]:
            objs.append({**o, "facts": [{**f, "review_status": "pending"} for f in o.get("facts", [])]})
        return {"objects": objs, "links": extraction.get("links", []),
                "_review": {"verified": False, "reason": reason, "all_pending": True}}
