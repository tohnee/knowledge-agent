"""WKA business layer · Action Engine — the ONLY legal write channel.
Every mutation (including ingest's candidate Objects/Links) goes through here so writes
are validated, permission-checked, bitemporally versioned, audited and written-back.

In-process reference (calls a KnowledgeStore directly). In production this is the
wka-action service (:8300) behind /api/v1/actions/*; the contract is identical:
  Step1 validate (perm + business rules) → Step2 sandbox (high-risk) →
  Step3 single-tx bitemporal write (append, never overwrite) → Step4 writeback + side-effects."""
from __future__ import annotations
import hashlib, json, time, uuid
from api.security.rbac import can_action

# action → (allowed roles, high-risk sandbox)
SPEC = {
    "create_object":   (["analyst", "strategy", "compliance"], False),  # ingest candidate intake
    "create_link":     (["analyst", "strategy", "compliance"], False),
    "review":          (["analyst", "strategy", "compliance"], False),
    "correct":         (["analyst", "strategy", "compliance"], False),
    "merge":           (["analyst", "strategy", "compliance"], False),
    "revise-capacity": (["analyst", "strategy", "compliance"], False),
    "advance-roadmap": (["analyst", "strategy", "compliance"], False),
    "promote":         (["strategy", "compliance"],            True),
    "mark":            (["compliance"],                        True),
}
ROADMAP_FSM = {"development": "risk", "risk": "HVM", "HVM": "EOL"}


class ActionError(Exception):
    pass


class ActionEngine:
    def __init__(self, store):
        self.store = store              # KnowledgeStore (adapter 3 backs this)
        self.audit: list = []           # in-process mirror for tests/UI

    def execute(self, name: str, params: dict, role: str, confirmed: bool = False,
                actor: str = "system", request_id: str | None = None) -> dict:
        spec = SPEC.get(name)
        if not spec:
            raise ActionError(f"unknown action {name}")
        roles, sandbox = spec

        # Step 1 — validation (permission + business rules)
        if role not in roles:
            raise ActionError(f"PERMISSION_DENIED: {role} cannot {name}")
        self._validate(name, params)

        # Step 2 — high-risk → sandbox preview (return impact, wait for confirm)
        if sandbox and not confirmed:
            return {"status": "pending_review",
                    "impact": {"affectedObjects": 1, "downstream": ["workshop", "ask", "ontology"],
                               "note": "high-risk — re-call with confirmed=True to commit"}}

        # Step 3 — single-transaction bitemporal write (append, never overwrite)
        result = self._apply(name, params)

        # Step 4 — writeback + durable audit (谁/何时/依据)
        audit = self._audit_record(name, params, role, actor, request_id, result)
        self.audit.append(audit)
        if hasattr(self.store, "append_audit"):
            self.store.append_audit(audit)
        return {"status": "executed", "action": name, "result": result, "audit_id": audit["id"]}

    def _audit_record(self, name, params, role, actor, request_id, result):
        canonical = json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)
        return {
            "id": request_id or str(uuid.uuid4()),
            "schemaVersion": 1,
            "action": name,
            "role": role,
            "actor": actor,
            "at": time.time(),
            "params": params,
            "paramsHash": hashlib.sha256(canonical.encode()).hexdigest(),
            "result": result,
            "decision": "executed",
        }

    def _validate(self, name, p):
        if name == "advance-roadmap":
            if ROADMAP_FSM.get(p.get("currentStatus")) != p.get("newStatus") and not p.get("correction"):
                raise ActionError("roadmap FSM violation (single-direction only)")
        if name in ("revise-capacity",) and p.get("sourceTier") == "rumor":
            raise ActionError("rumor must go to review queue, not direct write")

    def _apply(self, name, p):
        s = self.store
        if name == "create_object":
            existing = s.get_object(p["id"])
            if existing:                       # incremental merge, not duplicate
                s.merge_object(existing, p)
                return {"merged": p["id"]}
            s.put_object(p)
            return {"created": p["id"]}
        if name == "create_link":
            s.put_link(p)
            return {"linked": f"{p['src']}->{p['dst']}"}
        if name == "mark":
            s.set_controlled(p["entityId"], True, eccn=p.get("eccn", ""))
            return {"marked": p["entityId"]}
        if name == "advance-roadmap":
            s.append_status(p["nodeId"], p["newStatus"], p.get("eventDate", ""))
            return {"advanced": p["nodeId"], "to": p["newStatus"]}
        if name == "revise-capacity":
            s.append_capacity(p["fabId"], int(p["capacityWSPM"]), p["asOf"],
                              p.get("sourceTier", "official"), p.get("confidence", 0.9))
            return {"revised": p["fabId"]}
        if name == "review":
            s.bump_confidence(p["pageId"], 0.03)
            return {"reviewed": p["pageId"]}
        return {"noop": name}
