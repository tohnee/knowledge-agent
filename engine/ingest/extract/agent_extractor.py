"""WKA · AgentExtractor — production extraction engine for YOUR stack:
   Claude Code (orchestrator + self-validation) × DeepSeek/GLM (extraction backend, local).

Design (matches the scaling plan §1.1 tiered extraction + §9 controlled-not-out-of-domain):
  Tier-A  core (财报/标准/受控)  → Claude Code orchestrates, strong/local model, full extract + links
  Tier-B  general (海量)         → Claude Code orchestrates, DeepSeek/GLM light extract (cost driver)
  Tier-C  low/dup               → vectorize only (lazy), NO LLM call
  controlled (any tier)         → FORCED local model; egress gate makes leaving the boundary impossible

Output is the SAME 4-tuple StubExtractor returns, so GovernedIngest / Action / retrieval
/ security are unchanged. Drop-in swap in api/system.py.

Two ways Claude Code drives the model (configurable):
  · "claude_code" : shell out to `claude -p` which itself calls the model via MCP/tools
                    and returns validated JSON  (true "Claude Code 全程编排")
  · "direct"      : this class orchestrates (read→call DeepSeek/GLM→validate→retry) directly,
                    used when Claude Code is unavailable (e.g. CI). Same contract.
"""
from __future__ import annotations
import os, json, re, subprocess, hashlib
from common.models import Document, Chunk, WikiPage, Entity, Relation, Tier
from engine.ingest.extract.extractor import Extractor, _semantic_chunk, _sections
from engine.ingest.extract.classifier import TIER_BUDGET
from engine.ingest.extract.llm_client import OpenAICompatClient, default_endpoints, EgressViolation


def _sid(*parts): return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:12]

EXTRACT_SCHEMA_HINT = (
    '严格只输出 JSON（无 markdown 围栏、无解释）：'
    '{"objects":[{"id","type","interfaces":[],'
    '"facts":[{"key","value","unit","sourceTier","confidence","asOf","exportControlled"}]}],'
    '"links":[{"lt","from","to","card"}]}')


class AgentExtractor(Extractor):
    def __init__(self, llm: OpenAICompatClient | None = None,
                 semi_prompt_path: str = "/app/prompts/semi_enhanced.md",
                 orchestrator: str = "direct",        # "claude_code" | "direct"
                 strong_key: str = "deepseek-local",  # Tier-A backend (your strongest local)
                 light_key: str = "glm-local",        # Tier-B backend (cheap/light)
                 local_key: str = "deepseek-local",   # forced backend for controlled docs
                 max_retries: int = 2,
                 verifier=None,                       # ExtractionVerifier (self-critique 2nd gate)
                 verify: bool = True):
        self.llm = llm or OpenAICompatClient(default_endpoints())
        self.semi_prompt = open(semi_prompt_path).read() if os.path.exists(semi_prompt_path) else EXTRACT_SCHEMA_HINT
        self.orchestrator = orchestrator
        self.strong_key, self.light_key, self.local_key = strong_key, light_key, local_key
        self.max_retries = max_retries
        # second quality gate: Claude Code judges DeepSeek/GLM output
        if verify and verifier is None:
            from engine.ingest.extract.verifier import ExtractionVerifier
            verifier = ExtractionVerifier(llm=self.llm, orchestrator=orchestrator,
                                          judge_key=strong_key, local_judge_key=local_key)
        self.verifier = verifier if verify else None
        self.review = {"objects_dropped": 0, "facts_dropped": 0, "facts_pending_review": 0}

    # ── Extractor contract ──
    def extract(self, doc: Document, tier: Tier) -> dict:
        budget = TIER_BUDGET[tier]
        chunks = _semantic_chunk(doc)            # always produce child chunks (recall layer)

        if budget["model"] is None:              # Tier-C: lazy — vectorize only, no LLM
            return {"chunks": chunks, "wiki_pages": [], "entities": [], "relations": []}

        # pick backend by tier; controlled overrides to the forced-local backend
        endpoint_key = self.strong_key if budget["model"] == "strong" else self.light_key
        if doc.controlled:
            endpoint_key = self.local_key        # ← controlled forced local (egress gate also guards)

        objects, links = [], []
        for sec, body in _sections(doc.text):
            data = self._extract_chapter(body, doc, endpoint_key)
            # ── SECOND GATE: Claude Code self-critique of the extraction ──
            if self.verifier is not None and data.get("objects"):
                data = self.verifier.verify_extraction(body, data, controlled=doc.controlled)
                rv = data.get("_review", {})
                self.review["objects_dropped"] += rv.get("objects_dropped", 0)
                self.review["facts_dropped"] += rv.get("facts_dropped", 0)
                self.review["facts_pending_review"] += rv.get("facts_pending_review", 0)
            objects += data.get("objects", [])
            links += data.get("links", [])

        return self._assemble(doc, chunks, objects, links, budget)

    # ── chapter extraction: Claude Code orchestrates OR direct, both self-validate ──
    def _extract_chapter(self, chapter: str, doc: Document, endpoint_key: str) -> dict:
        prompt = (f"{self.semi_prompt}\n\nsourceTier={doc.source_tier.value}; "
                  f"as_of_hint={doc.meta.get('date','')}; controlled={doc.controlled}.\n"
                  f"{EXTRACT_SCHEMA_HINT}\n\n整章内容：\n{chapter}")

        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = (self._via_claude_code(prompt, doc.controlled)
                       if self.orchestrator == "claude_code"
                       else self._via_direct(prompt, endpoint_key, doc.controlled))
                data = self._parse_and_validate(raw)
                return data
            except EgressViolation:
                raise                              # never swallow an egress violation
            except Exception as e:                 # bad JSON / call failure → Claude Code self-corrects
                last_err = e
                prompt += f"\n\n上次输出无法解析（{e}）。请只返回合法 JSON，严格符合 schema。"
        # all retries failed → return empty (doc still has chunks/vectors; lazy-upgradable later)
        return {"objects": [], "links": [], "_error": str(last_err)}

    def _via_direct(self, prompt: str, endpoint_key: str, controlled: bool) -> str:
        return self.llm.chat(endpoint_key,
                             [{"role": "system", "content": "你是半导体知识抽取器。"},
                              {"role": "user", "content": prompt}],
                             controlled=controlled)

    def _via_claude_code(self, prompt: str, controlled: bool) -> str:
        """Claude Code 全程编排：claude -p drives the model (via its configured provider/MCP),
        does its own tool calls + self-validation, returns JSON. Model is swappable in
        Claude Code's settings (point it at the local DeepSeek/GLM OpenAI-compatible endpoint).
        Exact flags per your `claude --help`."""
        if controlled and os.getenv("CLAUDE_CODE_LOCAL_ONLY") != "1":
            # require Claude Code be configured against the LOCAL model for controlled docs
            raise EgressViolation("Claude Code must be in local-only mode (CLAUDE_CODE_LOCAL_ONLY=1) "
                                  "for controlled documents.")
        cmd = ["claude", "-p", "--output-format", "json", prompt]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            raise RuntimeError(f"claude code failed: {out.stderr[:200]}")
        payload = json.loads(out.stdout)
        return payload.get("result", out.stdout)

    # ── self-validation: parse, strip fences, check schema shape ──
    def _parse_and_validate(self, raw: str) -> dict:
        text = raw.strip().replace("```json", "").replace("```", "").strip()
        # tolerate leading prose: grab the outermost JSON object
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError("no JSON object found")
        data = json.loads(m.group(0))
        if not isinstance(data.get("objects"), list):
            raise ValueError("missing/invalid 'objects'")
        data.setdefault("links", [])
        # schema guard on each object
        for o in data["objects"]:
            if "id" not in o or "type" not in o:
                raise ValueError("object missing id/type")
            o.setdefault("facts", []); o.setdefault("interfaces", [])
        return data

    # ── map model JSON → engine 4-tuple (same shape as StubExtractor) ──
    def _assemble(self, doc, chunks, model_objects, model_links, budget) -> dict:
        wiki_pages, entities = [], []
        # one Wiki page per section (parent span); attach entities mentioned there
        sec_map = {}
        for sec, body in _sections(doc.text):
            pid = _sid(doc.id, sec)
            sec_map[sec] = pid
            if budget["build_wiki"] or budget["summarize"]:
                wiki_pages.append(WikiPage(id=pid, doc_id=doc.id, title=sec or doc.name,
                                           summary=body[:160], body=body, entities=[], section=sec))
        # re-point chunks to their section's wiki page
        for c in chunks:
            if c.section in sec_map:
                c.parent_id = sec_map[c.section]

        # model objects → engine Entities (id stable by name+type so resolver can merge)
        name_to_id = {}
        for o in model_objects:
            name = str(o.get("id") or o.get("title") or "")[:80]
            if not name:
                continue
            eid = _sid("ent", name, o["type"])
            name_to_id[name] = eid
            controlled = any(f.get("exportControlled") for f in o.get("facts", [])) or doc.controlled
            entities.append(Entity(id=eid, name=name, type=o["type"], doc_ids=[doc.id]))
        # attach entity names to wiki pages
        for w in wiki_pages:
            w.entities = [n for n in name_to_id if n in w.body]

        relations = []
        if budget["extract_relations"]:
            for l in model_links:
                relations.append(Relation(src=str(l.get("from", "")), dst=str(l.get("to", "")),
                                          lt=l.get("lt", "related"), doc_id=doc.id))

        return {"chunks": chunks, "wiki_pages": wiki_pages,
                "entities": _dedup(entities, doc.id), "relations": relations}


def _dedup(ents, doc_id):
    seen = {}
    for e in ents:
        if e.name not in seen:
            e.doc_ids = list(set(e.doc_ids + [doc_id]))
            seen[e.name] = e
    return list(seen.values())
