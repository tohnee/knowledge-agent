"""Runtime configuration and production-safe System wiring.

The zero-dependency test path still uses ``System()`` directly.  The HTTP gateway
uses this module so Docker/Compose and environment variables actually select the
intended production backend, authentication posture, and startup checks.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from api.system import System


class ProductionConfigError(RuntimeError):
    """Raised when the runtime environment is unsafe or incomplete."""


@dataclass(frozen=True)
class RuntimeConfig:
    env: str
    store_backend: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str
    auth_mode: str
    allow_role_header: bool
    jwt_secret: str
    cors_origins: tuple[str, ...]
    prefer_bge: bool
    use_hnsw: bool
    neo4j_init_schema: bool
    neo4j_init_retries: int
    neo4j_init_retry_seconds: float

    @property
    def production(self) -> bool:
        return self.env in {"prod", "production"}


def load_runtime_config() -> RuntimeConfig:
    """Load runtime configuration from environment variables."""
    env = _env("WKA_ENV", "dev").lower()
    return RuntimeConfig(
        env=env,
        store_backend=_env("WKA_STORE_BACKEND", "memory").lower(),
        neo4j_uri=_env("NEO4J_URI", "bolt://wka-neo4j:7687"),
        neo4j_user=_env("NEO4J_USER", "neo4j"),
        neo4j_password=_env("NEO4J_PASSWORD", ""),
        neo4j_database=_env("NEO4J_DATABASE", "neo4j"),
        auth_mode=_env("WKA_AUTH_MODE", "dev").lower(),
        allow_role_header=_bool("WKA_ALLOW_ROLE_HEADER", default=True),
        jwt_secret=_env("WKA_JWT_SECRET", ""),
        cors_origins=tuple(o.strip() for o in _env("WKA_CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",") if o.strip()),
        prefer_bge=_bool("WKA_PREFER_BGE", default=False),
        use_hnsw=_bool("WKA_USE_HNSW", default=True),
        neo4j_init_schema=_bool("WKA_NEO4J_INIT_SCHEMA", default=True),
        neo4j_init_retries=max(1, _int("WKA_NEO4J_INIT_RETRIES", 12)),
        neo4j_init_retry_seconds=max(0.0, _float("WKA_NEO4J_INIT_RETRY_SECONDS", 2.0)),
    )


def validate_runtime_config(cfg: RuntimeConfig) -> None:
    """Fail fast for settings that are unsafe on a public production service."""
    if cfg.store_backend not in {"memory", "neo4j"}:
        raise ProductionConfigError("WKA_STORE_BACKEND must be either 'memory' or 'neo4j'")
    if cfg.auth_mode not in {"dev", "jwt"}:
        raise ProductionConfigError("WKA_AUTH_MODE must be either 'dev' or 'jwt'")
    if not cfg.cors_origins:
        raise ProductionConfigError("WKA_CORS_ORIGINS must contain at least one origin")

    if not cfg.production:
        return

    if cfg.auth_mode != "jwt":
        raise ProductionConfigError("production requires WKA_AUTH_MODE=jwt")
    if cfg.allow_role_header:
        raise ProductionConfigError("production requires WKA_ALLOW_ROLE_HEADER=0")
    if _weak_secret(cfg.jwt_secret):
        raise ProductionConfigError("production requires a strong, non-default WKA_JWT_SECRET")
    if cfg.store_backend != "neo4j":
        raise ProductionConfigError("production requires WKA_STORE_BACKEND=neo4j")
    if _weak_secret(cfg.neo4j_password):
        raise ProductionConfigError("production requires a strong, non-default NEO4J_PASSWORD")
    for origin in cfg.cors_origins:
        if origin == "*":
            raise ProductionConfigError("production CORS cannot allow '*'")
        host = (urlparse(origin).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
            raise ProductionConfigError("production CORS origins must not be localhost-only")


def build_system_from_env() -> tuple[System, RuntimeConfig]:
    """Build the API System from env, including Neo4j driver/schema wiring."""
    cfg = load_runtime_config()
    validate_runtime_config(cfg)

    if cfg.store_backend == "neo4j":
        driver = _build_neo4j_driver(cfg)
        sys_ = System(
            store_backend="neo4j",
            neo4j_driver=driver,
            prefer_bge=cfg.prefer_bge,
            use_hnsw=cfg.use_hnsw,
        )
        if cfg.neo4j_database and hasattr(sys_.store, "database"):
            sys_.store.database = cfg.neo4j_database
        if cfg.neo4j_init_schema:
            _init_neo4j_schema_with_retry(sys_, cfg)
        return sys_, cfg

    return System(prefer_bge=cfg.prefer_bge, use_hnsw=cfg.use_hnsw), cfg


def _build_neo4j_driver(cfg: RuntimeConfig):
    try:
        from neo4j import GraphDatabase
    except Exception as exc:  # pragma: no cover - depends on optional package
        raise ProductionConfigError("Neo4j backend requires: pip install -r requirements-neo4j.txt") from exc
    return GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password))


def _init_neo4j_schema_with_retry(sys_: System, cfg: RuntimeConfig) -> None:
    last_exc: Exception | None = None
    for attempt in range(1, cfg.neo4j_init_retries + 1):
        try:
            sys_.store.init_schema()
            return
        except Exception as exc:  # pragma: no cover - real network/service timing
            last_exc = exc
            if attempt == cfg.neo4j_init_retries:
                break
            time.sleep(cfg.neo4j_init_retry_seconds)
    raise ProductionConfigError(f"Neo4j schema initialization failed after {cfg.neo4j_init_retries} attempts: {last_exc}")


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        raise ProductionConfigError(f"{name} must be an integer")


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        raise ProductionConfigError(f"{name} must be a number")


def _weak_secret(value: str) -> bool:
    normalized = value.strip().lower()
    return len(value.strip()) < 16 or normalized in {
        "change-me",
        "change-me-dev-secret",
        "secret",
        "password",
        "neo4j",
    }
