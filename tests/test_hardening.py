"""Production hardening checks for auth, cache invalidation, audit persistence, and filtered HNSW."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys

from api.security.auth import authenticate_authorization, AuthError
from api.system import System
from common.models import Document, SourceTier
from engine.infra.vector_store import VectorStore

P, F = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
res = []
def ck(n, c, d=""):
    res.append(c); print(f"  {P if c else F} {n}" + (f"  ({d})" if d else ""))


def _jwt(claims, secret="secret"):
    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    head = b64({"alg": "HS256", "typ": "JWT"})
    body = b64(claims)
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"

print("\n=== Auth hardening ===")
os.environ["WKA_AUTH_MODE"] = "jwt"
os.environ["WKA_ALLOW_ROLE_HEADER"] = "0"
os.environ["WKA_JWT_SECRET"] = "secret"
p = authenticate_authorization("Bearer " + _jwt({"sub": "u1", "roles": ["compliance"]}))
ck("JWT maps server-side role", p.role == "compliance", p.subject)
denied = False
try:
    authenticate_authorization("Role compliance")
except AuthError:
    denied = True
ck("Role header denied in jwt mode", denied)
os.environ.pop("WKA_AUTH_MODE", None)
os.environ.pop("WKA_ALLOW_ROLE_HEADER", None)
os.environ.pop("WKA_JWT_SECRET", None)

print("\n=== Cache invalidation + durable audit ===")
sys_ = System()
sys_.ingest_doc(Document("h1", "a.md", "# A\nN3 状态 HVM。", SourceTier.OFFICIAL))
first = sys_.ask("N3 状态", role="analyst")
second = sys_.ask("N3 状态", role="analyst")
ck("retriever cache hit before write", second["cache_hit"] is True)
sys_.ingest_doc(Document("h2", "b.md", "# B\nN5 状态 HVM。", SourceTier.OFFICIAL))
third = sys_.ask("N3 状态", role="analyst")
ck("ingest invalidates cache", third["cache_hit"] is False)
a = sys_.run_action("revise-capacity", {"fabId": "fab-h", "capacityWSPM": 1, "asOf": "2026"}, role="analyst", actor="u1")
ck("action returns audit id", bool(a.get("audit_id")))
ck("store persists audit", len(sys_.store.list_audit()) >= 1)

print("\n=== Filter-aware HNSW ===")
vs = VectorStore(quantize=False, use_hnsw=True)
class C:
    def __init__(self, i, dept):
        self.id=f"c{i}"; self.embedding=[1.0, i/1000.0]; self.parent_id="p"; self.text=str(i); self.doc_id="d"; self.meta={"department": dept, "controlled": False}
chunks=[C(i, "a" if i % 2 else "b") for i in range(60)]
vs.upsert_chunks(chunks)
hits = vs.search([1.0, 0.01], k=5, filt=lambda m: m.get("department") == "a")
ck("filtered HNSW returns eligible rows", len(hits) > 0 and all(h[2]["department"] == "a" for h in hits), f"hits={len(hits)}")

print("\n" + "=" * 46)
ok = sum(res)
print(f"  HARDENING: {ok}/{len(res)} passed")
print("=" * 46)
sys.exit(0 if ok == len(res) else 1)
