# Repository Documentation Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add repository-quality baseline files for `knowledge-agent`, including `.gitignore`, `LICENSE`, a formalized `README.md`, and a deployment manual grounded in the current codebase and integration docs.

**Architecture:** This change is documentation-first. It does not alter runtime behavior. The work packages the existing fused architecture into a repo-facing narrative: repository metadata at the root, a clearer onboarding flow in `README.md`, and a deployment document that explains dependency choices, runtime modes, service boundaries, environment variables, verification, and production swap points already present in the code and docs.

**Tech Stack:** Git, Markdown, Python project conventions, FastAPI runtime notes, Neo4j integration notes

---

### Task 1: Add Repository Baseline Files

**Files:**
- Create: `.gitignore`
- Create: `LICENSE`

**Step 1: Inspect the repo for generated artifacts**

Run: `find . -name '__pycache__' -o -name '*.pyc' -o -name '.DS_Store'`
Expected: identify Python and macOS junk patterns worth ignoring.

**Step 2: Write a minimal Python-focused `.gitignore`**

Include entries for:

```gitignore
__pycache__/
*.py[cod]
.DS_Store
.pytest_cache/
.venv/
venv/
dist/
build/
*.egg-info/
.env
```

**Step 3: Add the MIT license text**

Create `LICENSE` using the standard MIT template with the current copyright holder.

**Step 4: Verify the new files are visible to git**

Run: `git status --short`
Expected: `.gitignore` and `LICENSE` appear as new tracked files; ignored junk does not.

### Task 2: Restructure the Root README

**Files:**
- Modify: `README.md`

**Step 1: Preserve the current technical substance**

Carry forward the existing fused-system explanation, six-seam model, runtime modes, and production swap table so no architectural knowledge is lost.

**Step 2: Rewrite into a formal repository structure**

Use sections like:

```markdown
# Knowledge Agent
## Overview
## Architecture
## Repository Layout
## Quick Start
## Running Tests
## API and Frontend
## Storage Backends
## Security Model
## Deployment Docs
```

**Step 3: Add explicit references to the deeper docs**

Link to:
- `NEO4J_INTEGRATION.md`
- `SCALING_INTEGRATION.md`
- `VERIFY_AND_EMBEDDING.md`
- `DEPLOYMENT.md`

**Step 4: Verify readability**

Read the final `README.md` and confirm that a new visitor can answer:
- what the project is
- how to run it locally
- how to test it
- where deployment guidance lives

### Task 3: Add a Deployment Manual

**Files:**
- Create: `DEPLOYMENT.md`

**Step 1: Ground the manual in actual code paths**

Base the document on:
- `api/main.py`
- `api/system.py`
- `NEO4J_INTEGRATION.md`
- `SCALING_INTEGRATION.md`
- `VERIFY_AND_EMBEDDING.md`

**Step 2: Document supported deployment modes**

Include:
- zero-dependency local/headless mode
- FastAPI HTTP mode
- Neo4j-backed production mode
- optional advanced extractor / embedder mode

**Step 3: Document environment, startup, and verification**

Include exact examples for:

```bash
python -m tests.test_closed_loop
uvicorn api.main:app --port 8000
python -m tests.test_neo4j_parity
python -m tests.test_http
```

Also explain required Python packages for each mode.

**Step 4: Add operational notes**

Cover:
- CORS and demo auth limitations
- role header vs JWT in production
- Neo4j schema initialization
- local-only handling for controlled documents
- what is still a placeholder versus production-ready

### Task 4: Verify and Summarize

**Files:**
- Test: `README.md`
- Test: `DEPLOYMENT.md`
- Test: `.gitignore`
- Test: `LICENSE`

**Step 1: Run documentation verification checks**

Run:

```bash
git status --short
python3 - <<'PY'
from pathlib import Path
for p in ["README.md", "DEPLOYMENT.md", ".gitignore", "LICENSE"]:
    print(p, Path(p).exists(), Path(p).stat().st_size)
PY
```

Expected: all files exist and are non-empty.

**Step 2: Check markdown-facing files for diagnostics**

Use editor diagnostics on:
- `README.md`
- `DEPLOYMENT.md`

Expected: no obvious markdown or file-level diagnostics.

**Step 3: Review git diff**

Run: `git diff -- README.md DEPLOYMENT.md .gitignore LICENSE`
Expected: only documentation/repository metadata changes.
