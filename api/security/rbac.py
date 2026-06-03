"""WKA business layer · Dynamic Security (RBAC + OPA field-level + Vault decrypt).
This is the AUTHORITATIVE security boundary. The engine's router does a coarse
pre-filter; the FINAL decision (clear/summary/masked/hidden) is here.

Synchronous reference implementation (OPA/Vault calls stubbed as pure functions so the
whole闭环 runs in tests). In production swap `_opa_decide`/`_vault_decrypt` for httpx
calls to OPA(:8281) and Vault(:8200) — signatures unchanged."""
from __future__ import annotations
import base64, json, os, urllib.request

ROLES = {
    "viewer":     {"actions": []},
    "analyst":    {"actions": ["review", "correct", "merge"]},
    "strategy":   {"actions": ["review", "correct", "merge", "promote"]},
    "compliance": {"actions": ["review", "correct", "merge", "promote", "mark"]},
}


def can_action(role: str, action: str) -> bool:
    return action in ROLES.get(role, {}).get("actions", [])


def _opa_decide(role: str, controlled: bool) -> str:
    """Return field visibility. Uses OPA when WKA_OPA_URL is set; otherwise local policy.

    OPA failures are fail-closed for controlled content so a policy outage cannot leak
    plaintext. Non-controlled content remains clear.
    """
    if not controlled:
        return "clear"
    opa_url = os.getenv("WKA_OPA_URL", "").rstrip("/")
    if opa_url:
        try:
            payload = json.dumps({"input": {"role": role, "controlled": controlled}}).encode()
            req = urllib.request.Request(
                opa_url + "/v1/data/wka/field_visibility", data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=float(os.getenv("WKA_OPA_TIMEOUT", "2"))) as r:
                data = json.loads(r.read())
            decision = data.get("result")
            if decision in {"clear", "summary", "masked", "hidden"}:
                return decision
        except Exception:
            return "hidden"
    return {"compliance": "clear", "strategy": "summary",
            "analyst": "masked", "viewer": "hidden"}.get(role, "hidden")


def _vault_decrypt(ciphertext: str) -> str:
    vault_url = os.getenv("WKA_VAULT_URL", "").rstrip("/")
    token = os.getenv("WKA_VAULT_TOKEN", "")
    if not vault_url:
        return ciphertext
    try:
        payload = json.dumps({"ciphertext": ciphertext}).encode()
        req = urllib.request.Request(
            vault_url + "/v1/transit/decrypt/wka", data=payload,
            headers={"Content-Type": "application/json", "X-Vault-Token": token})
        with urllib.request.urlopen(req, timeout=float(os.getenv("WKA_VAULT_TIMEOUT", "2"))) as r:
            data = json.loads(r.read())
        plaintext = data.get("data", {}).get("plaintext", "")
        return base64.b64decode(plaintext).decode() if plaintext else ciphertext
    except Exception:
        # Fail closed for encrypted fields: never expose ciphertext as if it were cleartext.
        return "[解密失败]"


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
