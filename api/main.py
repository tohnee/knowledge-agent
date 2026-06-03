"""WKA-Fused · API Gateway — the single entry point the frontend HTML talks to.
Routes map 1:1 to the six views. Uses the composition-root System so ingest, retrieval,
Action and security are all the same shared instances.

Run:  uvicorn api.main:app --port 8000
(FastAPI optional — the System works headless; see tests/test_closed_loop.py)"""
from __future__ import annotations

try:
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    _HAS_FASTAPI = True
except Exception:
    _HAS_FASTAPI = False

from api.system import System
from api.security.rbac import ROLES, apply_field_security
from common.models import Document, SourceTier

SYS = System()


def _role(authorization: str) -> str:
    # prod: decode JWT; here trust a header for demo. NEVER let frontend self-assign in prod.
    if authorization and authorization.startswith("Role "):
        r = authorization[5:].strip()
        return r if r in ROLES else "viewer"
    return "viewer"


if _HAS_FASTAPI:
    app = FastAPI(title="Workspace Knowledge Agent (fused)", version="1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/v1/health")
    def health(): return {"status": "ok", "service": "wka-fused"}

    # ① Sources — upload + build wiki (governed ingest)
    @app.post("/api/v1/documents/upload")
    def upload(body: dict, authorization: str = Header(default="")):
        doc = Document(id=body["id"], name=body["name"], text=body["text"],
                       source_tier=SourceTier(body.get("sourceTier", "analyst")),
                       controlled=body.get("controlled", False),
                       meta={"department": body.get("department", "default")})
        return SYS.ingest_doc(doc)

    # ② Wiki / Object — get object with field-level security
    @app.get("/api/v1/objects/{oid}")
    def get_object(oid: str, authorization: str = Header(default="")):
        role = _role(authorization)
        o = SYS.store.get_object(oid)
        if not o:
            raise HTTPException(404)
        if o.get("controlled") and role == "viewer":
            raise HTTPException(403, "controlled object")
        facts = []
        for f in o.get("facts", []):
            disp, masked = apply_field_security(role, f["value"], f.get("controlled", False))
            if disp is None:
                continue
            facts.append({**f, "value": disp, "masked": masked})
        return {**o, "facts": facts, "allowedActions": ROLES[role]["actions"]}

    # ② as-of bitemporal
    @app.get("/api/v1/objects/{oid}/asof")
    def asof(oid: str, year: int):
        return {"known": SYS.store.capacity_asof(oid, year), "truth": SYS.store.capacity_truth(oid)}

    # ④ Workshop — actions queue / Functions
    @app.get("/api/v1/actions/pending")
    def pending():
        return {"controls": SYS.ingest.pending_controls, "audit_count": len(SYS.actions.audit)}

    # Actions (write channel) — permission is enforced authoritatively by the Action engine
    @app.post("/api/v1/actions/{name}")
    def run_action(name: str, body: dict, authorization: str = Header(default="")):
        role = _role(authorization)
        try:
            return SYS.run_action(name, body, role, confirmed=body.get("_confirmed", False))
        except Exception as e:
            msg = str(e)
            code = 403 if "PERMISSION_DENIED" in msg else 422
            raise HTTPException(code, msg)

    # ⑤ Ask — grounded QA (engine funnel + OPA/Vault)
    @app.post("/api/v1/knowledge/qa")
    def qa(body: dict, authorization: str = Header(default="")):
        return SYS.ask(body["question"], role=_role(authorization), dept=body.get("department"))
