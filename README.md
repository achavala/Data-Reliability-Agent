# DRA MVP Scaffold

Data Reliability Agent MVP scaffold for dbt incident triage and approval-gated remediation.

## What is implemented

- `POST /ingest/dbt_run`: ingest dbt `manifest` + `run_results`; create incident on failure.
- `POST /agent/run`: retrieve evidence, triage root-cause hypotheses, propose remediation patch, run safety/validation, update incident status.
- `POST /approvals`: record human approval/rejection.
- Postgres-backed incident + audit trail storage.
- Qdrant evidence indexing (lightweight deterministic embeddings).
- Minimal eval harness (`eval/score.py`).

## Stack

- FastAPI
- Postgres 16
- Qdrant
- Python 3.11+

## Quick start

1. Start infra:

```bash
docker compose up -d
```

2. Create env and install deps:

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Run API:

```bash
uvicorn app.main:app --reload --port 8000
```

4. Run demo flow:

```bash
bash scripts/demo.sh
```

## Eval harness

```bash
python eval/score.py
```

## Notes

- LLM behavior is mocked with deterministic heuristics for reproducibility.
- `validate_patch` currently runs static checks only. You can replace this with real `dbt compile`/`dbt test` tool calls in a sandboxed project checkout.
- Slack and GitHub integrations are intentionally not wired yet; this scaffold is the backend control loop and persistence base.
