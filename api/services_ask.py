"""ADAPTER 2 · Retrieval → Security → Answer  (powers the frontend Ask view)
================================================================================
The engine's Retriever runs the three-stage funnel (route→recall+RRF→rerank→compress).
Its router does a COARSE controlled pre-filter, but the AUTHORITATIVE decision lives in
the business layer (OPA/Vault). So after the funnel, every context block passes through
`apply_field_security` — the frontend NEVER receives unauthorized plaintext.

`answer()` is what POST /api/v1/knowledge/qa calls."""
from __future__ import annotations
from engine.retrieval.retriever import Retriever
from api.security.rbac import apply_field_security, ROLES


class GroundedQA:
    def __init__(self, retriever: Retriever, generate_fn=None):
        self.retriever = retriever
        self.generate = generate_fn or _stub_generate

    def answer(self, question: str, role: str = "analyst", department: str | None = None) -> dict:
        # 1) engine funnel (coarse controlled pre-filter happens inside via router)
        r = self.retriever.retrieve(question, role=role, department=department)

        # 2) AUTHORITATIVE security pass — OPA/Vault on every context block
        secured, dropped, masked_count = [], 0, 0
        for ctx in r["contexts"]:
            controlled = bool(ctx.get("controlled", False))
            display, masked = apply_field_security(role, ctx["text"], controlled)
            if display is None:                 # hidden for this role
                dropped += 1
                continue
            if masked:
                masked_count += 1
            secured.append({**ctx, "text": display, "masked": masked})

        # the engine router may also have pre-filtered controlled rows for this role
        # (e.g. viewer); reflect that any controlled content was withheld.
        any_controlled_withheld = dropped > 0 or masked_count > 0

        # 3) ground the answer on secured contexts, with citations
        answer_text = self.generate(question, secured)
        citations = [c for c in r.get("citations", [])]

        return {
            "answer": answer_text,
            "mode": r.get("mode"),
            "contexts": secured,
            "citations": citations,
            "filtered": any_controlled_withheld,
            "masked_by_security": masked_count,
            "dropped_by_security": dropped,
            "dropped_low_confidence": r.get("dropped_low_confidence", 0),
            "grounded": len(secured) > 0,
            "timing": r.get("timing", {}),
            "cache_hit": r.get("cache_hit", False),
        }


def _stub_generate(question: str, contexts: list) -> str:
    """Deterministic grounded generation for the closed loop test.
    Prod: route to Claude (or local model if any context is controlled) with the
    secured contexts + citation instructions."""
    if not contexts:
        return "（在你被授权访问的知识范围内未找到可接地的内容。）"
    bits = "；".join(c["text"][:60] for c in contexts[:3])
    return f"基于授权知识作答：{bits}"
