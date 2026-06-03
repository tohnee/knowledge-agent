"""WKA-Fused · API Gateway — the single entry point the frontend HTML talks to.
Routes map 1:1 to the six views. Uses the composition-root System so ingest, retrieval,
Action and security are all the same shared instances.

Run:  uvicorn api.main:app --port 8000
(FastAPI optional — the System works headless; see tests/test_closed_loop.py)"""
from __future__ import annotations

import os

try:
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from api.schemas import ActionRequest, DocumentUpload, QuestionRequest
    _HAS_FASTAPI = True
except Exception:
    _HAS_FASTAPI = False

from api.config import ProductionConfigError, build_system_from_env
from api.security.auth import AuthError, authenticate_authorization
from api.security.rbac import ROLES, apply_field_security
from common.models import Document, SourceTier

try:
    SYS, RUNTIME_CONFIG = build_system_from_env()
except ProductionConfigError as exc:
    raise RuntimeError(f"invalid runtime configuration: {exc}") from exc


def _principal(authorization: str):
    try:
        return authenticate_authorization(authorization)
    except AuthError as e:
        if _HAS_FASTAPI:
            raise HTTPException(401, str(e))
        raise


def _role(authorization: str) -> str:
    """Backward-compatible helper used by tests; real API routes call _principal()."""
    return _principal(authorization).role


def _cors_origins() -> list[str]:
    raw = os.getenv("WKA_CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")
    return [o.strip() for o in raw.split(",") if o.strip()]


if _HAS_FASTAPI:
    app = FastAPI(title="Workspace Knowledge Agent (fused)", version="1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
    )

    @app.get("/api/v1/health")
    def health():
        return {
            "status": "ok",
            "service": "wka-fused",
            "env": RUNTIME_CONFIG.env,
            "auth_mode": RUNTIME_CONFIG.auth_mode,
            "store_backend": RUNTIME_CONFIG.store_backend,
            "hnsw": RUNTIME_CONFIG.use_hnsw,
        }

    # ① Sources — upload + build wiki (governed ingest)
    @app.post("/api/v1/documents/upload")
    def upload(body: DocumentUpload, authorization: str = Header(default="")):
        principal = _principal(authorization)
        doc = Document(id=body.id, name=body.name, text=body.text,
                       source_tier=SourceTier(body.sourceTier),
                       controlled=body.controlled,
                       meta={"department": body.department})
        return SYS.ingest_doc(doc, actor=principal.subject)

    # ② Wiki / Object — get object with field-level security
    @app.get("/api/v1/objects/{oid}")
    def get_object(oid: str, authorization: str = Header(default="")):
        role = _principal(authorization).role
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
    def run_action(name: str, body: ActionRequest, authorization: str = Header(default="")):
        principal = _principal(authorization)
        params = body.action_params()
        try:
            return SYS.run_action(name, params, principal.role,
                                  confirmed=body.confirmed, actor=principal.subject)
        except Exception as e:
            msg = str(e)
            code = 403 if "PERMISSION_DENIED" in msg else 422
            raise HTTPException(code, msg)

    # ⑤ Ask — grounded QA (engine funnel + OPA/Vault)
    @app.post("/api/v1/knowledge/qa")
    def qa(body: QuestionRequest, authorization: str = Header(default="")):
        principal = _principal(authorization)
        return SYS.ask(body.question, role=principal.role, dept=body.department)
