# Knowledge Agent

Knowledge Agent is a fused workspace knowledge system that combines:

- a lightweight browser frontend
- a WKA business core with Action-based writes, security policy enforcement, and bitemporal state
- a retrieval and ingest engine with tiered extraction, multi-stage recall, reranking, and HNSW-backed vector search

The project is intentionally built around three integration seams rather than three isolated subsystems. The core value is that ingest, retrieval, action, and security all run through one shared object graph.

## Overview

At a high level, the system works like this:

```text
frontend  -->  /api/v1/*  -->  WKA business core  -->  retrieval + ingest engine
                                 |                      |
                                 | writes via Action    | shared vector / BM25 / graph / wiki stores
                                 v                      v
                           ontology + audit + security + bitemporal history
```

Key properties:

- `wka-scale` is not a replacement. It is embedded as the ingest and retrieval engine inside the WKA business core.
- Action execution remains the only write channel for ontology mutations.
- Retrieval and ingest share the same engine-side stores, which is what makes the fused architecture work.
- Security filtering stays authoritative at the business layer, not inside the retrieval engine.

## Architecture

The repo is organized around four responsibilities:

1. `api/` exposes the FastAPI gateway and composition root.
2. `adapters/` holds the three critical seams between the engine and the WKA domain.
3. `action_engine/` owns audited writes, permission checks, and bitemporal updates.
4. `engine/` provides tiered extraction, retrieval funneling, vector infrastructure, and scaling primitives.

The three named seams are:

- `adapters/model_map.py`: engine dataclasses to ontology storage shape
- `api/services_ask.py`: retriever output to policy-enforced grounded answering
- `adapters/governed_ingest.py`: extraction results to Action-governed writes

## Quick Start

This repository supports several runtime profiles.

### 1. Zero-dependency core verification

Runs the fused architecture in memory without FastAPI, Neo4j, or external model dependencies:

```bash
python -m tests.test_closed_loop
python -m tests.test_neo4j_parity
```

### 2. Optional HTTP mode

Install the minimal API dependencies:

```bash
pip install fastapi uvicorn httpx
```

Start the API server:

```bash
uvicorn api.main:app --port 8000
```

Then open `frontend/index.html` in a browser. The demo client talks to `/api/v1/*`.

### 3. Optional production-oriented storage mode

To switch ontology storage from the in-memory backend to Neo4j, inject a Neo4j driver into `System(...)`. See [NEO4J_INTEGRATION.md](file:///Users/tc/Workspace/github/knowledge-agent/NEO4J_INTEGRATION.md) for the exact wiring and schema notes.

## Running Tests

Core test commands:

```bash
python -m tests.test_closed_loop
python -m tests.test_neo4j_parity
python -m tests.test_http
python -m tests.test_agent_extractor
python -m tests.test_verifier
python -m tests.bench_embedding
```

Notes:

- `test_closed_loop` validates the fused seams end to end against the memory backend.
- `test_neo4j_parity` checks contract parity between the memory store and the Neo4j store.
- `test_http` exercises the real FastAPI path when the optional HTTP dependencies are present.
- extractor and verifier tests validate the optional advanced ingest stack.

## Repository Layout

```text
knowledge-agent/
├── frontend/                  Browser UI and API client
├── api/                       FastAPI gateway and composition root
├── adapters/                  Integration seams between engine and domain model
├── action_engine/             Audited write path and knowledge store backends
├── engine/                    Ingest, retrieval, and vector infrastructure
├── common/                    Shared dataclasses
├── tests/                     Closed-loop, HTTP, parity, and extractor verification
├── NEO4J_INTEGRATION.md       Neo4j backend and contract notes
├── SCALING_INTEGRATION.md     Scaling architecture and advanced extractor wiring
├── VERIFY_AND_EMBEDDING.md    Self-critique and embedding notes
└── DEPLOYMENT.md              Deployment and operational runbook
```

## Core Data Flow

```text
Document upload
  -> GovernedIngest.ingest()
  -> IngestPipeline builds chunks / wiki / entities / relations
  -> model_map translates engine entities into ontology candidates
  -> ActionEngine executes create or merge actions
  -> KnowledgeStore persists ontology objects and bitemporal facts
  -> controlled signals may enter the pending controls queue

Question answering
  -> GroundedQA.answer()
  -> Retriever routes, recalls, reranks, and compresses contexts
  -> field-level security is applied per returned fact
  -> grounded answer and references are returned to the frontend

Any write path
  -> ActionEngine.execute()
  -> permission check -> sandbox for risky actions -> audited bitemporal write
```

## Validated Fusion Seams

The closed-loop tests explicitly verify these seams:

| Seam | Implementation | What is validated |
| --- | --- | --- |
| Ingest writes ontology only through Action | `adapters/governed_ingest.py` | ontology writes are audited and role-scoped |
| Incremental merge avoids duplicate objects | governed ingest + merge logic | repeated entities merge instead of duplicating |
| Controlled documents do not auto-mark themselves | governed ingest pending controls | export-control actions require explicit review |
| Retrieval reads the same engine stores that ingest fills | `api/system.py` | ingest and retrieval share one engine object graph |
| Security filtering remains authoritative | `api/services_ask.py`, `api/security/rbac.py` | facts are shown, masked, or dropped by role |
| Action enforces permission and bitemporal semantics | `action_engine/engine.py` | restricted actions and as-of behavior are enforced |

## Security Model

The security model is intentionally layered:

- API requests derive a role from a demo header in local mode.
- `GroundedQA` applies field-level filtering before answer assembly.
- Action execution is the authoritative enforcement point for write permissions.
- Controlled content is treated specially throughout ingest and answer generation.

Important local-vs-production distinction:

- In local mode, `api/main.py` trusts a `Role ...` header for demo purposes.
- In production, the repo expects that to be replaced with JWT decoding and real identity propagation.

## Storage Backends

Two knowledge store backends exist behind the same contract:

- `InMemoryKnowledgeStore` for tests and zero-dependency development
- `Neo4jKnowledgeStore` for production-oriented graph persistence and bitemporal history

The rest of the system is designed to remain unchanged when swapping between the two.

## Production Swap Points

The codebase already names the main placeholders you would replace in production:

| Reference implementation | Production direction |
| --- | --- |
| `HashEmbedder` | BGE-M3 or another real embedding backend |
| `StubExtractor` | `AgentExtractor` or another real LLM-backed extractor |
| `LexicalCrossEncoder` | a stronger reranker such as MS MARCO cross-encoder |
| in-memory vector store | Qdrant, Milvus, or another persistent vector backend |
| in-memory knowledge store | Neo4j plus a wiki/document backend |
| local role header | JWT-backed authn/authz |

## Documentation Map

For deeper details, start here:

- [DEPLOYMENT.md](file:///Users/tc/Workspace/github/knowledge-agent/DEPLOYMENT.md): deployment profiles, dependencies, environment variables, verification, and production notes
- [NEO4J_INTEGRATION.md](file:///Users/tc/Workspace/github/knowledge-agent/NEO4J_INTEGRATION.md): Neo4j schema, parity model, and integration details
- [SCALING_INTEGRATION.md](file:///Users/tc/Workspace/github/knowledge-agent/SCALING_INTEGRATION.md): scaling pillars, advanced extraction, and model orchestration
- [VERIFY_AND_EMBEDDING.md](file:///Users/tc/Workspace/github/knowledge-agent/VERIFY_AND_EMBEDDING.md): self-critique pipeline and embedding notes

## Current Status

This repository already demonstrates a real fused architecture, but not every component is production-ready out of the box.

Today, the strongest ready-to-run pieces are:

- the fused composition root
- the Action-based write discipline
- in-memory closed-loop verification
- the Neo4j contract and parity tests
- the FastAPI gateway demo path

The main productionization work still expected by the code and docs is:

- real auth instead of demo role headers
- concrete external policy and secret backends
- a production embedding model
- a production extractor and verifier runtime
- deployment packaging around the service graph
