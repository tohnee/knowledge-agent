"""WKA business layer · Dynamic Security (RBAC + OPA field-level + Vault decrypt).
This is the AUTHORITATIVE security boundary. The engine's router does a coarse
pre-filter; the FINAL decision (clear/summary/masked/hidden) is here.

Synchronous reference implementation (OPA/Vault calls stubbed as pure functions so the
whole闭环 runs in tests). In production swap `_opa_decide`/`_vault_decrypt` for httpx
calls to OPA(:8281) and Vault(:8200) — signatures unchanged."""
from __future__ import annotations

ROLES = {
    "viewer":     {"actions": []},
    "analyst":    {"actions": ["review", "correct", "merge"]},
    "strategy":   {"actions": ["review", "correct", "merge", "promote"]},
    "compliance": {"actions": ["review", "correct", "merge", "promote", "mark"]},
}


def can_action(role: str, action: str) -> bool:
    return action in ROLES.get(role, {}).get("actions", [])


def _opa_decide(role: str, controlled: bool) -> str:
    """Mirror of opa-policies/wka.rego field_visibility. clear|summary|masked|hidden."""
    if not controlled:
        return "clear"
    return {"compliance": "clear", "strategy": "summary",
            "analyst": "masked", "viewer": "hidden"}.get(role, "hidden")


def _vault_decrypt(ciphertext: str) -> str:
    # prod: POST Vault /v1/transit/decrypt/wka — here pass-through
    return ciphertext


def field_visible(role: str, controlled: bool) -> str:
    return _opa_decide(role, controlled)


def apply_field_security(role: str, value, controlled: bool, encrypted: bool = False):
    """Return (display_value, masked_bool) for ONE field. Frontend never gets
    unauthorized plaintext — masking happens server-side here."""
    vis = _opa_decide(role, controlled)
    if vis == "clear":
        return (_vault_decrypt(value) if encrypted else value, False)
    if vis == "summary":
        return ("[受控摘要] " + str(value)[:8] + "…", True)
    if vis == "masked":
        return ("•••• 受控字段", True)
    return (None, True)  # hidden


def object_visible(role: str, controlled: bool) -> bool:
    """Row-level: can this role see the object at all?"""
    return (not controlled) or role != "viewer"
