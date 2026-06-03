"""Pillar 1 · §1.2 Multi-level products extraction.
ONE ingest produces several index structures: child chunks + Wiki page + entities/
relations. Parent-Child: embed small chunks (precise recall), return parent span.

`Extractor` is the interface. `ClaudeExtractor` shows the real Claude Code + llm-wiki
call. `StubExtractor` is deterministic (no network) so the pipeline + tests run anywhere."""
from __future__ import annotations
import re, json, subprocess, os, hashlib
from abc import ABC, abstractmethod
from common.models import Document, Chunk, WikiPage, Entity, Relation, Tier
from engine.ingest.extract.classifier import TIER_BUDGET


def _sid(*parts) -> str:
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:12]


class Extractor(ABC):
    @abstractmethod
    def extract(self, doc: Document, tier: Tier) -> dict:
        """Return {chunks, wiki_pages, entities, relations}."""


# ── Production: Claude Code + llm-wiki skill (whole-chapter, no pre-chunk) ──
class ClaudeExtractor(Extractor):
    def __init__(self, semi_prompt_path="/app/prompts/semi_enhanced.md", local_url=None):
        self.semi_prompt = open(semi_prompt_path).read() if os.path.exists(semi_prompt_path) else ""
        self.local_url = local_url

    def extract(self, doc: Document, tier: Tier) -> dict:
        budget = TIER_BUDGET[tier]
        if budget["model"] is None:                  # Tier-C lazy: vectorize only
            return {"chunks": self._chunk(doc), "wiki_pages": [], "entities": [], "relations": []}
        prompt = (f"使用 llm-wiki skill 对整章内容做抽取，sourceTier={doc.source_tier.value}。"
                  f"严格输出 JSON：{{objects:[...],links:[...]}}\n\n{doc.text}")
        cmd = ["claude", "-p", "--output-format", "json",
               "--append-system-prompt", self.semi_prompt, prompt]
        env = dict(os.environ)
        if doc.controlled and self.local_url:        # 受控不出域
            env["ANTHROPIC_BASE_URL"] = self.local_url
        out = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
        data = json.loads(json.loads(out.stdout).get("result", out.stdout)
                          .replace("```json", "").replace("```", "").strip())
        return self._assemble(doc, data, budget)

    def _chunk(self, doc): return _semantic_chunk(doc)
    def _assemble(self, doc, data, budget):
        raise NotImplementedError(
            "ClaudeExtractor is deprecated and intentionally not production-wired. "
            "Use engine.ingest.extract.agent_extractor.AgentExtractor, which implements "
            "schema validation, verifier gating, and controlled-content egress rules.")


# ── Test/dev: deterministic, network-free ──
class StubExtractor(Extractor):
    """Produces stable multi-level products from text structure — for pipeline + tests."""
    def extract(self, doc: Document, tier: Tier) -> dict:
        budget = TIER_BUDGET[tier]
        chunks = _semantic_chunk(doc)

        wiki_pages, entities, relations = [], [], []
        if budget["build_wiki"] or budget["summarize"]:
            # one Wiki page per top section (the parent span)
            for sec, body in _sections(doc.text):
                pid = _sid(doc.id, sec)
                ents = _find_entities(body)
                wiki_pages.append(WikiPage(
                    id=pid, doc_id=doc.id, title=sec or doc.name,
                    summary=body[:160], body=body, entities=[e.name for e in ents], section=sec))
                # re-point this section's chunks to this wiki page (parent-child)
                for c in chunks:
                    if c.section == sec:
                        c.parent_id = pid
                entities += ents

        if budget["extract_relations"]:
            # naive co-occurrence relations between entities in same section
            for sec, body in _sections(doc.text):
                ents = [e.name for e in _find_entities(body)]
                for i in range(len(ents)):
                    for j in range(i + 1, len(ents)):
                        relations.append(Relation(src=ents[i], dst=ents[j],
                                                  lt="co_occurs", doc_id=doc.id))
        return {"chunks": chunks, "wiki_pages": wiki_pages,
                "entities": _dedup_entities(entities, doc.id), "relations": relations}


# ── helpers (shared) ──
def _sections(text: str):
    """Split on markdown headings (semantic chunking, not arbitrary char counts)."""
    parts = re.split(r"\n(?=#{1,3}\s)", text.strip())
    out = []
    for p in parts:
        m = re.match(r"#{1,3}\s*(.+)", p)
        title = m.group(1).strip() if m else ""
        body = p[m.end():].strip() if m else p.strip()
        if body:
            out.append((title, body))
    return out or [("", text.strip())]


def _semantic_chunk(doc: Document, max_len=500, overlap=80) -> list:
    """Split on meaning (sections), then size-cap with overlap. Returns child chunks."""
    chunks = []
    for sec, body in _sections(doc.text):
        i = 0
        while i < len(body):
            piece = body[i:i + max_len]
            cid = _sid(doc.id, sec, i)
            chunks.append(Chunk(id=cid, doc_id=doc.id, text=piece,
                                parent_id=_sid(doc.id, sec), section=sec,
                                meta={"source_tier": doc.source_tier.value,
                                      "controlled": doc.controlled, "doc_name": doc.name}))
            if i + max_len >= len(body):
                break
            i += max_len - overlap
    return chunks


# tiny entity recognizer: semiconductor patterns + capitalized/CJK proper-noun-ish tokens
_ENT_PATTERNS = [
    (r"\bN[0-9]+[A-Z]?\b", "ProcessNode"),
    (r"\bHBM[0-9]?[A-Z]?\b", "Standard"),
    (r"\bEUV\b|\bDUV\b", "Equipment"),
    (r"\bJESD[0-9]+\b", "Standard"),
]
_COMPANY_HINTS = ["代工厂", "设备商", "公司", "Foundry", "TSMC", "ASML", "长鑫", "Fabless"]


def _find_entities(text: str) -> list:
    found = {}
    for pat, typ in _ENT_PATTERNS:
        for m in re.findall(pat, text):
            found.setdefault(m, Entity(id=_sid("ent", m), name=m, type=typ))
    for hint in _COMPANY_HINTS:
        if hint in text:
            found.setdefault(hint, Entity(id=_sid("ent", hint), name=hint, type="Company"))
    return list(found.values())


def _dedup_entities(ents, doc_id):
    seen = {}
    for e in ents:
        if e.name not in seen:
            e.doc_ids = [doc_id]
            seen[e.name] = e
    return list(seen.values())
