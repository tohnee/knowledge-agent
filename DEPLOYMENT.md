# Deployment Guide

This guide explains how to run and deploy `knowledge-agent` based on the code that exists today.

It covers four operating profiles:

1. zero-dependency local verification
2. FastAPI HTTP service mode
3. Neo4j-backed deployment mode
4. advanced extractor and embedder mode

The repo does not yet ship with a lockfile, container image, or infra templates, so this document focuses on runtime wiring, dependency choices, environment variables, and verification steps.

## 1. Deployment Profiles

### Profile A: Zero-dependency local mode

Use this when you want to verify the fused architecture without external services.

Characteristics:

- uses `InMemoryKnowledgeStore`
- uses the default in-process `System()`
- uses stub or hash-based components where the code allows it
- does not require FastAPI, Neo4j, or external LLM services

Primary verification:

```bash
python -m tests.test_closed_loop
python -m tests.test_neo4j_parity
```

This is the best starting point for validating the business seams before you deploy any service.

### Profile B: FastAPI service mode

Use this when you want a real HTTP entry point for the browser frontend or another client.

Required packages:

```bash
pip install fastapi uvicorn httpx
```

Start the server:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Useful endpoint examples:

- `GET /api/v1/health`
- `POST /api/v1/documents/upload`
- `GET /api/v1/objects/{oid}`
- `POST /api/v1/actions/{name}`
- `POST /api/v1/knowledge/qa`

Frontend notes:

- `frontend/index.html` is a static browser UI.
- `frontend/wka-client.js` expects the API to be reachable at the same origin or a compatible base URL.
- local CORS is permissive by default in `api/main.py`.

### Profile C: Neo4j-backed deployment mode

Use this when you want the ontology and bitemporal write path to persist in Neo4j instead of memory.

Required packages:

```bash
pip install fastapi uvicorn httpx neo4j
```

Runtime wiring comes from `api/system.py`:

```python
from neo4j import GraphDatabase
from api.system import System

driver = GraphDatabase.driver("bolt://wka-neo4j:7687", auth=("neo4j", "PASSWORD"))
sys_ = System(store_backend="neo4j", neo4j_driver=driver)
sys_.store.init_schema()
```

Important behavior:

- the rest of the object graph stays the same
- `ActionEngine` still owns write discipline
- `Retriever` still reads the engine-side shared stores
- only the knowledge store backend changes

See also: [NEO4J_INTEGRATION.md](file:///Users/tc/Workspace/github/knowledge-agent/NEO4J_INTEGRATION.md)

### Profile D: Advanced extractor and embedder mode

Use this when you want real extractor orchestration, self-critique, and stronger embeddings.

Optional package families:

```bash
pip install neo4j
pip install FlagEmbedding
# or
pip install sentence-transformers
```

The advanced path is documented in:

- [SCALING_INTEGRATION.md](file:///Users/tc/Workspace/github/knowledge-agent/SCALING_INTEGRATION.md)
- [VERIFY_AND_EMBEDDING.md](file:///Users/tc/Workspace/github/knowledge-agent/VERIFY_AND_EMBEDDING.md)

This mode is optional. The repo is still runnable without it.

## 2. Runtime Components

The runtime is anchored by the `System` composition root in [system.py](file:///Users/tc/Workspace/github/knowledge-agent/api/system.py).

`System(...)` wires together:

- an embedder
- an extractor
- the Action engine
- the knowledge store backend
- governed ingest
- the retrieval pipeline
- grounded question answering

Operationally, this means:

- ingest and retrieval must be instantiated from the same `System`
- writes should go through `System.run_action(...)`
- if you split services later, preserve the "shared store + authoritative write channel" contract

## 3. Environment Variables

The repo ships with `.env.example`, and the documentation references additional environment values used by optional integrations.

Recommended baseline variables:

```bash
NEO4J_URI=bolt://wka-neo4j:7687
NEO4J_PASSWORD=change-me
```

Advanced extractor and local-model variables mentioned by the current docs:

```bash
DEEPSEEK_LOCAL_URL=http://vllm:8000/v1
DEEPSEEK_MODEL=deepseek-v3
GLM_LOCAL_URL=http://ollama:11434/v1
GLM_MODEL=glm-4
LOCAL_LLM_HOSTS=vllm,ollama,wka-vllm
CLAUDE_CODE_LOCAL_ONLY=1
```

Operational meaning:

- `NEO4J_URI` and `NEO4J_PASSWORD` support the graph backend
- `LOCAL_LLM_HOSTS` and `CLAUDE_CODE_LOCAL_ONLY` are part of the local-only guardrail for controlled documents
- the DeepSeek and GLM variables describe how the optional extractor stack talks to local OpenAI-compatible endpoints

## 4. Local Bring-up

### Minimal verification

Run the no-dependency tests first:

```bash
python -m tests.test_closed_loop
python -m tests.test_neo4j_parity
```

### HTTP verification

Install the API dependencies and run:

```bash
pip install fastapi uvicorn httpx
python -m tests.test_http
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Then open the frontend manually:

1. open `frontend/index.html`
2. point it at the local API
3. use a demo role such as `analyst`
4. exercise upload, query, object retrieval, and action flows

## 5. Neo4j Deployment Procedure

When moving from memory to Neo4j, use this order:

1. provision a reachable Neo4j instance
2. install the `neo4j` Python driver
3. construct `System(store_backend="neo4j", neo4j_driver=driver)`
4. run `sys_.store.init_schema()`
5. execute parity verification

Suggested verification:

```bash
python -m tests.test_neo4j_parity
python -m tests.test_closed_loop
```

Important implementation notes from the current code and docs:

- object facts are modeled as `:Fact` nodes, not nested node properties
- link type is stored as a property to avoid dynamic Cypher injection
- bitemporal observations are append-only
- `capacity_asof` and `capacity_truth` semantics are intentionally distinct

## 6. Security and Auth Notes

There is a sharp distinction between demo mode and production mode.

### What the code does today

In [main.py](file:///Users/tc/Workspace/github/knowledge-agent/api/main.py):

- local auth trusts a header like `Role analyst`
- CORS is configured with `allow_origins=["*"]`
- controlled facts may be masked or dropped depending on role

### What production should do

Before exposing the service publicly, replace or front these behaviors:

- replace demo role headers with JWT or another real identity mechanism
- restrict CORS to known origins
- ensure the frontend cannot self-assign roles
- back policy and decryption hooks with real OPA and Vault implementations

## 7. Controlled Data Handling

The docs and code make one operational rule very clear:

- controlled content must not silently flow to non-local model endpoints

The optional advanced extractor stack uses local-only guardrails for that path. If you enable advanced extraction:

- keep controlled documents on local model infrastructure
- set `CLAUDE_CODE_LOCAL_ONLY=1` for controlled runs
- treat any egress violation as a hard deployment failure

## 8. Placeholders vs Production-Ready Components

The repository already contains production-oriented contracts, but not every default implementation is meant for direct production use.

Reasonably production-shaped:

- composition root pattern
- governed ingest flow
- Action-based write discipline
- Neo4j knowledge store contract
- bitemporal behavior
- field-level security flow

Still placeholder or intentionally simplified by default:

- header-based demo auth
- permissive CORS
- hash-based default embeddings
- stub extractor as the no-dependency default
- local test/demo-oriented packaging

## 9. Verification Checklist

Use this checklist after any deployment change.

### Core verification

```bash
python -m tests.test_closed_loop
python -m tests.test_neo4j_parity
```

### API verification

```bash
python -m tests.test_http
curl http://localhost:8000/api/v1/health
```

### Optional advanced verification

```bash
python -m tests.test_agent_extractor
python -m tests.test_verifier
python -m tests.bench_embedding
```

Interpretation:

- if the closed-loop tests fail, the fused seams are broken
- if the parity tests fail, the Neo4j backend no longer matches the memory contract
- if the HTTP tests fail, the browser/API path is broken
- if the advanced tests fail, keep the service on the simpler defaults until fixed

## 10. Recommended Deployment Path

For the least risky rollout, use this sequence:

1. validate the repo in zero-dependency mode
2. add the FastAPI service path
3. switch the knowledge store to Neo4j
4. harden auth and CORS
5. enable advanced extractor and embedder components only after the base service is stable

That order matches the current architecture: first preserve the fused seams, then swap the implementation details behind them.
