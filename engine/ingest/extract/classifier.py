"""Pillar 1 · §1.1 Tiered Extraction — cheap classifier routes each doc to a tier.
The single biggest waste at scale is running expensive full LLM extraction on EVERY
doc. Tier-A gets full compile, Tier-B light, Tier-C lazy (vectorize only).

In production the value/type signal can come from a small model; here we use fast
deterministic rules (source_tier + doc_type keywords + length) so it runs anywhere."""
from common.models import Document, Tier, SourceTier

# doc-type keywords → high value (worth full compile)
HIGH_VALUE_KEYWORDS = (
    "财报", "earnings", "10-k", "招股", "prospectus", "合同", "contract",
    "标准", "jedec", "semi", "白皮书", "techbrief", "annual report", "法说",
)
LOW_VALUE_KEYWORDS = ("草稿", "draft", "转发", "fwd:", "re:", "邮件", "便签", "note")

# domain entity hints — a short doc that mentions these still has signal (→ Tier-B not Tier-C)
ENTITY_HINTS = ("n3", "n5", "n2", "euv", "duv", "hbm", "jesd", "代工厂", "设备商", "晶圆", "fab", "foundry")


def classify(doc: Document) -> Tier:
    """Return extraction Tier. Pure function — easy to unit-test."""
    text_l = (doc.name + " " + doc.text[:2000]).lower()

    # Rule 1: official + high-value type → Tier-A (full compile)
    if doc.source_tier == SourceTier.OFFICIAL and _has(text_l, HIGH_VALUE_KEYWORDS):
        return Tier.A
    # Rule 2: explicit low-value markers (draft/forward) → Tier-C (lazy)
    if _has(text_l, LOW_VALUE_KEYWORDS):
        return Tier.C
    # Rule 3: rumor without high-value type → Tier-C
    if doc.source_tier == SourceTier.RUMOR and not _has(text_l, HIGH_VALUE_KEYWORDS):
        return Tier.C
    # Rule 4: official (any) or high-value type → Tier-A
    if doc.source_tier == SourceTier.OFFICIAL or _has(text_l, HIGH_VALUE_KEYWORDS):
        return Tier.A
    # Rule 5: very short AND mentions no domain entities → Tier-C (truly low signal)
    if len(doc.text) < 200 and not _has(text_l, ENTITY_HINTS):
        return Tier.C
    # Default: general analyst/news → Tier-B (light entity extraction)
    return Tier.B


def _has(text: str, kws) -> bool:
    return any(k in text for k in kws)


# Budget table — cost knobs the worker reads (model choice, whether to build wiki)
TIER_BUDGET = {
    Tier.A: {"model": "strong",  "build_wiki": True,  "extract_relations": True,  "summarize": True},
    Tier.B: {"model": "small",   "build_wiki": False, "extract_relations": True,  "summarize": True},
    Tier.C: {"model": None,      "build_wiki": False, "extract_relations": False, "summarize": False},
}
