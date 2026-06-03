"""Authentication helpers for the HTTP gateway.

Production mode uses bearer JWTs whose claims are mapped to server-side roles.  The
legacy ``Authorization: Role <role>`` header is retained only as an explicit local
/dev compatibility mode so the zero-dependency tests and demo frontend can run
without an identity provider.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from api.security.rbac import ROLES


class AuthError(Exception):
    """Raised when the caller cannot be authenticated or mapped to a role."""


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    claims: dict[str, Any]
    auth_mode: str


def authenticate_authorization(authorization: str | None) -> Principal:
    """Authenticate an Authorization header and return a server-mapped principal.

    Supported modes:
    - ``Bearer <jwt>``: validates HS256 when ``WKA_JWT_SECRET`` is set, otherwise
      accepts unsigned/dev JWTs only when ``WKA_AUTH_MODE=dev``.
    - ``Role <role>``: local compatibility mode only. Disable with
      ``WKA_ALLOW_ROLE_HEADER=0`` or force JWT with ``WKA_AUTH_MODE=jwt``.
    """
    header = (authorization or "").strip()
    auth_mode = os.getenv("WKA_AUTH_MODE", "dev").strip().lower()

    if header.startswith("Bearer "):
        claims = _decode_jwt(header[7:].strip(), auth_mode=auth_mode)
        role = _role_from_claims(claims)
        return Principal(
            subject=str(claims.get("sub") or claims.get("email") or "jwt-user"),
            role=role,
            claims=claims,
            auth_mode="jwt",
        )

    if header.startswith("Role ") and _role_header_allowed(auth_mode):
        requested = header[5:].strip()
        role = requested if requested in ROLES else "viewer"
        return Principal(subject=f"local:{role}", role=role, claims={"role": role}, auth_mode="dev-role")

    if auth_mode == "jwt":
        raise AuthError("missing bearer token")

    # Local/dev default: unauthenticated callers are viewers.
    return Principal(subject="anonymous", role="viewer", claims={}, auth_mode="anonymous-dev")


def _role_header_allowed(auth_mode: str) -> bool:
    if auth_mode == "jwt":
        return False
    return os.getenv("WKA_ALLOW_ROLE_HEADER", "1").strip().lower() not in {"0", "false", "no"}


def _role_from_claims(claims: dict[str, Any]) -> str:
    for key in ("wka_role", "role"):
        role = claims.get(key)
        if isinstance(role, str) and role in ROLES:
            return role
    roles = claims.get("roles") or claims.get("groups") or []
    if isinstance(roles, str):
        roles = [roles]
    for role in ("compliance", "strategy", "analyst", "viewer"):
        if role in roles:
            return role
    return "viewer"


def _decode_jwt(token: str, auth_mode: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("invalid jwt shape")
    header = _json_b64(parts[0])
    claims = _json_b64(parts[1])
    secret = os.getenv("WKA_JWT_SECRET", "")

    if secret:
        alg = header.get("alg")
        if alg != "HS256":
            raise AuthError(f"unsupported jwt alg {alg!r}; expected HS256")
        expected = hmac.new(secret.encode(), f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).digest()
        actual = _b64decode(parts[2])
        if not hmac.compare_digest(expected, actual):
            raise AuthError("invalid jwt signature")
    elif auth_mode == "jwt":
        raise AuthError("WKA_JWT_SECRET is required in jwt mode")

    exp = claims.get("exp")
    if exp is not None and float(exp) < time.time():
        raise AuthError("jwt expired")
    return claims


def _json_b64(data: str) -> dict[str, Any]:
    try:
        return json.loads(_b64decode(data).decode())
    except Exception as exc:
        raise AuthError("invalid jwt json") from exc


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
