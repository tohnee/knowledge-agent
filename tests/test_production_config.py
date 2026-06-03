"""Runtime configuration checks for production-safe API startup."""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager

from api.config import ProductionConfigError, load_runtime_config, validate_runtime_config
from api.system import System

P, F = "\033[92m✓\033[0m", "\033[91m✗\033[0m"
res = []


def ck(n, c, d=""):
    res.append(c)
    print(f"  {P if c else F} {n}" + (f"  ({d})" if d else ""))


@contextmanager
def env(**updates):
    keys = set(updates)
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k, v in updates.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


print("\n=== Runtime config defaults ===")
with env(
    WKA_ENV=None,
    WKA_STORE_BACKEND=None,
    WKA_AUTH_MODE=None,
    WKA_ALLOW_ROLE_HEADER=None,
    WKA_JWT_SECRET=None,
    WKA_CORS_ORIGINS=None,
):
    cfg = load_runtime_config()
    validate_runtime_config(cfg)
    sys_ = System(prefer_bge=cfg.prefer_bge, use_hnsw=cfg.use_hnsw)
    ck("dev defaults stay zero-dependency", cfg.env == "dev" and cfg.store_backend == "memory")
    ck("dev System still builds in memory", sys_.store.__class__.__name__ == "InMemoryKnowledgeStore")

print("\n=== Production fail-fast checks ===")
with env(
    WKA_ENV="production",
    WKA_STORE_BACKEND="memory",
    WKA_AUTH_MODE="dev",
    WKA_ALLOW_ROLE_HEADER="1",
    WKA_JWT_SECRET="secret",
    WKA_CORS_ORIGINS="http://localhost:8000",
    NEO4J_PASSWORD="neo4j",
):
    rejected = False
    try:
        validate_runtime_config(load_runtime_config())
    except ProductionConfigError as e:
        rejected = "WKA_AUTH_MODE=jwt" in str(e)
    ck("production rejects dev auth", rejected)

with env(
    WKA_ENV="production",
    WKA_STORE_BACKEND="neo4j",
    WKA_AUTH_MODE="jwt",
    WKA_ALLOW_ROLE_HEADER="0",
    WKA_JWT_SECRET="a-strong-random-jwt-secret-2026",
    WKA_CORS_ORIGINS="https://app.example.com",
    NEO4J_PASSWORD="a-strong-neo4j-password-2026",
):
    cfg = load_runtime_config()
    try:
        validate_runtime_config(cfg)
        ok = True
    except ProductionConfigError:
        ok = False
    ck("production accepts strict public settings", ok)

with env(
    WKA_ENV="production",
    WKA_STORE_BACKEND="neo4j",
    WKA_AUTH_MODE="jwt",
    WKA_ALLOW_ROLE_HEADER="0",
    WKA_JWT_SECRET="a-strong-random-jwt-secret-2026",
    WKA_CORS_ORIGINS="*",
    NEO4J_PASSWORD="a-strong-neo4j-password-2026",
):
    rejected = False
    try:
        validate_runtime_config(load_runtime_config())
    except ProductionConfigError as e:
        rejected = "CORS" in str(e)
    ck("production rejects wildcard CORS", rejected)

print("\n" + "=" * 46)
ok = sum(res)
print(f"  PRODUCTION CONFIG: {ok}/{len(res)} passed")
print("=" * 46)
sys.exit(0 if ok == len(res) else 1)
