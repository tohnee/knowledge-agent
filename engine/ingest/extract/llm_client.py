"""WKA · OpenAI-compatible LLM client for local DeepSeek / GLM (vLLM / Ollama).

Your stack: models served behind an OpenAI-compatible /v1/chat/completions endpoint.
One client, model name swaps DeepSeek↔GLM. Claude Code is the orchestrator (separate);
this is the *extraction backend model* Claude Code drives.

HARD EGRESS GATE (export-control): a `controlled` request may ONLY hit a LOCAL endpoint
that is on the allow-list. Any attempt to route controlled content to a non-local base_url
raises EgressViolation — it is NOT a warning, it cannot be bypassed by config."""
from __future__ import annotations
import os, json, re, urllib.request
from dataclasses import dataclass


class EgressViolation(Exception):
    """Raised when controlled content would leave the local boundary. Never caught silently."""


def _is_local(base_url: str) -> bool:
    """A base_url counts as local only if its host is loopback / private / on the allow-list.
    Allow-list is explicit env (LOCAL_LLM_HOSTS, comma-sep) plus loopback names."""
    host = re.sub(r"^https?://", "", base_url).split("/")[0].split(":")[0].lower()
    allow = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal",
             "vllm", "ollama", "wka-vllm", "wka-ollama"}
    allow |= {h.strip().lower() for h in os.getenv("LOCAL_LLM_HOSTS", "").split(",") if h.strip()}
    if host in allow:
        return True
    # private RFC1918 ranges
    if host.startswith(("10.", "192.168.")):
        return True
    if host.startswith("172."):
        try:
            return 16 <= int(host.split(".")[1]) <= 31
        except (ValueError, IndexError):
            return False
    return False


@dataclass
class LLMEndpoint:
    name: str               # logical name e.g. "deepseek-local" / "glm-local" / "deepseek-cloud"
    base_url: str           # OpenAI-compatible base, e.g. http://vllm:8000/v1
    model: str              # served model id, e.g. "deepseek-v3" / "glm-4"
    local: bool             # is this a local (in-boundary) endpoint?
    api_key: str = "not-needed-for-local"


class OpenAICompatClient:
    """Minimal OpenAI-compatible chat client (stdlib only — no openai dep required).
    In prod you may swap the transport for the `openai` SDK; the contract is identical."""

    def __init__(self, endpoints: dict[str, LLMEndpoint]):
        self.endpoints = endpoints

    def chat(self, endpoint_key: str, messages: list, *, controlled: bool = False,
             temperature: float = 0.0, max_tokens: int = 4096, timeout: int = 600) -> str:
        ep = self.endpoints[endpoint_key]

        # ── HARD EGRESS GATE ──
        if controlled and not ep.local:
            raise EgressViolation(
                f"controlled content routed to non-local endpoint '{ep.name}' ({ep.base_url}); "
                f"export-controlled documents must use a local model. Refusing.")
        if controlled and not _is_local(ep.base_url):
            raise EgressViolation(
                f"endpoint '{ep.name}' base_url {ep.base_url} is not on the local allow-list; "
                f"refusing controlled request.")

        body = json.dumps({"model": ep.model, "messages": messages,
                           "temperature": temperature, "max_tokens": max_tokens}).encode()
        req = urllib.request.Request(
            ep.base_url.rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {ep.api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]


def default_endpoints() -> dict[str, LLMEndpoint]:
    """Build endpoints from env. Local vLLM/Ollama serving DeepSeek + GLM.
    Cloud entries (if any) are flagged local=False so the gate blocks controlled content."""
    return {
        "deepseek-local": LLMEndpoint(
            "deepseek-local",
            os.getenv("DEEPSEEK_LOCAL_URL", "http://host.docker.internal:8000/v1"),
            os.getenv("DEEPSEEK_MODEL", "deepseek-v3"), local=True),
        "glm-local": LLMEndpoint(
            "glm-local",
            os.getenv("GLM_LOCAL_URL", "http://host.docker.internal:8001/v1"),
            os.getenv("GLM_MODEL", "glm-4"), local=True),
    }
