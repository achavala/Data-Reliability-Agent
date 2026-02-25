# Data Reliability Agent (DRA) — Project Summary

Detailed status of what is completed, pending, next steps, end-to-end flow, and tech components per phase.

---

## 1. What Is Completed

### 1.1 Core API & Ingest
- **`POST /ingest/dbt_run`** — Ingest dbt `manifest` + `run_results`; creates incident when status is `error`/`failed`/`fail`.
  - Pipeline upsert (`dim_pipeline`), run insert (`fact_pipeline_run`), incident creation (`fact_incident`), audit event.
- **`POST /agent/run`** — Runs triage (evidence → hypotheses → remediation → validation), stores agent output on incident, supports approval-gated status (`blocked` / `awaiting_approval` / `approved`).
- **`POST /approvals`** — Records human approval/rejection; updates incident status to `approved` or `rejected` and writes to `approvals` table.
- **`GET /health`** — Health check.

### 1.2 Persistence (Postgres)
- **Schema** (in `app/db.py`):
  - `dim_pipeline`, `fact_pipeline_run`, `fact_incident`, `audit_event`, `approvals`
  - `incident_pr`, `incident_notification`, `dim_dataset_scd2`, `agent_trace`
- **Operations**: init_db, upsert_pipeline, insert_pipeline_run, create_incident, get_run_by_incident, update_incident_agent_output, insert_audit_event, add_approval, get_manifest_by_run_id, insert/get_incident_pr, insert/get_notifications, insert/get_agent_traces, get_dataset_schema_history.

### 1.3 Evidence & Vector Store (Qdrant)
- **VectorStore** (`app/vector_store.py`): Qdrant client, collection creation with configurable embedding dimension.
- **Embeddings**: OpenAI embeddings when `OPENAI_API_KEY` set; else deterministic hash-based embeddings (reproducible).
- **Indexing**: `upsert_evidence`, `upsert_triage_result`, `upsert_dbt_docs`; `search` and `search_similar_incidents` (doc_type `triage`).
- Evidence and triage results are upserted after each agent run for future similarity search.

### 1.4 Agent Logic
- **Dual mode**:
  - **LLM mode** (when `ANTHROPIC_API_KEY` set and `MOCK_LLM=false`): ReAct-style loop with Claude; native tool_use; tools: `retrieve_evidence`, `query_lineage`, `search_similar_incidents`, `propose_patch`; structured JSON output parsed for triage + remediation.
  - **Heuristic mode** (default): `retrieve_evidence` → `triage` (schema_drift vs transformation_logic_error) → `propose_remediation` → `validate_patch`; all deterministic.
- **Tools** (`app/tools.py`): Schemas and `execute_tool` for all four tools; lineage uses `LineageGraph` from run manifest; vector_store for similar incidents.
- **Validation** (`app/agent.py` + `app/dbt_validator.py`): Static safety checks (no DROP/TRUNCATE/DELETE); optional real dbt: sandbox copy of project, apply patch, `dbt compile` + `dbt test` when `DBT_PROJECT_DIR` set.

### 1.5 Lineage (M6)
- **LineageGraph** (`app/lineage.py`): Built from manifest `nodes`, `sources`, `exposures`, `metrics` and `depends_on`; legacy `parent_map`/`child_map` supported.
  - BFS upstream/downstream, blast_radius (impacted models, exposures, metrics), `to_serializable`, `detect_schema_drift` (SCD2).
- **API**: `GET /lineage?model_id=&run_id=` (upstream/downstream), `GET /lineage/blast-radius?model_id=&run_id=&max_depth=`.

### 1.6 GitHub PR (M3)
- **`app/github.py`**: Branch creation, commit patch via Contents API, create PR, get PR status + check runs; `format_pr_body` with triage/remediation/validation.
- **API**: `POST /incidents/{id}/pr` (create PR from incident’s patch/triage/remediation/validation), `GET /incidents/{id}/pr` (PR status).
- **DB**: `incident_pr` stores pr_number, pr_url, branch_name, status. PR creation requires `GITHUB_TOKEN` and `GITHUB_REPO`.

### 1.7 Slack (M4)
- **`app/slack.py`**: Block Kit message with root cause, blast radius, validation, optional PR link; Approve/Reject buttons.
  - `post_incident_notification`: post to channel (when `SLACK_BOT_TOKEN` set); `insert_notification` + audit.
  - `handle_slack_interaction`: parse payload, call `add_approval` and audit for approve/reject.
- **API**: `POST /webhooks/slack` — handles interactive payload (approve/reject); Slack not wired to auto-post on incident creation (see Pending).

### 1.8 Eval Harness
- **`eval/score.py`**: Heuristic predictor `predict_cause` (schema drift, freshness, transformation logic); metrics: precision/recall/F1, patch_quality_score, blast_radius_accuracy.
- **Modes**: `quick` (heuristic-only on messages), `full` (ingest → agent run → score triage/patch/blast), `regression` (vs baseline traces in `eval/traces/`).
- **Data**: `eval/incidents.jsonl` — 20 incidents with message, expected_cause, optional expected_patch_contains, expected_blast_radius_min, run_results, manifest.

### 1.9 Config & Infra
- **`app/config.py`**: All settings via env (DB, Qdrant, MOCK_LLM, Anthropic, OpenAI embeddings, GitHub, Slack, dbt paths).
- **Docker Compose**: Postgres 16 (port 5433), Qdrant (6333); volumes for data.
- **Demo**: `scripts/demo.sh` — ingest sample_ingest.json → agent/run → approvals; `scripts/sample_ingest.json` single failing run.

---

## 2. What Is Pending / Not Wired

- **Slack auto-notify**: No automatic call to `post_incident_notification` when an incident is created or when agent run completes; webhook only handles button clicks. Pending: trigger notification from ingest or agent_run and store `message_ts`/channel in `incident_notification`.
- **GitHub/Slack “not wired”**: README states “Slack and GitHub integrations are intentionally not wired yet” in the sense of **automated flow**: e.g. auto-create PR after approval, auto-post to Slack on incident. The **code** for GitHub PR and Slack messaging exists; the **orchestration** (when to create PR, when to post to Slack) is not hooked into the main flow.
- **Real dbt validation in default path**: `validate_patch` uses real `dbt compile`/`dbt test` only when `DBT_PROJECT_DIR` (and optionally model_path/model_id) are set; otherwise static checks only. So “replace with real dbt … in sandbox” is optional and env-dependent.
- **Agent trace persistence**: `agent_trace` table and `insert_agent_trace`/`get_agent_traces` exist in DB; the LLM agent loop does not write to `agent_trace` (only to `audit_event`). So structured agent traces are not yet populated by the agent.
- **Regression eval baseline**: Regression mode expects `eval/traces/*.json`; that directory may not exist or be populated; no automated baseline capture script mentioned.
- **Model path for PR**: `create_incident_pr_endpoint` uses a default `models/{incident_id}.sql`; true model path from triage/failed node is not fully derived for all cases.

---

## 3. Next Steps (Recommended)

1. **Orchestration**
   - After `agent_run` completes with status `awaiting_approval`, optionally call `post_incident_notification` (if Slack configured) and/or auto-create PR via `create_pr_for_incident` (if GitHub configured).
   - After `POST /approvals` with `approve`, optionally trigger PR creation if not already created, and optionally post “Approved” to Slack thread.

2. **Agent traces**
   - In the LLM agent loop, after each iteration (and tool call), call `insert_agent_trace` with step_index, step_type (e.g. `llm_call`, `tool_retrieve_evidence`), input/output, model_name, token_usage, latency_ms. Expose `GET /incidents/{id}/traces` for debugging.

3. **Eval**
   - Add a small script or `eval/score.py` subcommand to capture current agent outputs as baseline traces for regression (e.g. write `eval/traces/{incident_id}.json`).
   - Run `full` mode against a running API and track metrics over time (e.g. in CI).

4. **Validation**
   - When a real dbt project is available, set `DBT_PROJECT_DIR` (and optionally `DBT_PROFILES_DIR`) and derive `model_path` from manifest (e.g. `nodes[unique_id].original_file_path`) so PR and validation use the real path.

5. **Documentation**
   - Document required Slack app scopes and interactivity URL for `POST /webhooks/slack`.
   - Document GitHub repo permissions and branch protection if used.

---

## 4. End-to-End Steps Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. INGEST                                                                   │
│    CI/orchestrator sends dbt manifest + run_results (failed run)           │
│    → POST /ingest/dbt_run                                                   │
│    → Pipeline upserted, run stored, incident created (if status failed)     │
│    → incident_id returned                                                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. AGENT RUN                                                                │
│    POST /agent/run { incident_id, approval_required }                       │
│    → Load run (manifest + run_results) from DB                              │
│    → Run agent loop (LLM or heuristic):                                     │
│        • Retrieve evidence (failed nodes, messages, lineage, schema signals) │
│        • Query lineage / blast radius                                       │
│        • Search similar incidents (Qdrant)                                  │
│        • Propose patch                                                      │
│    → Validate patch (static safety + optional dbt compile/test)             │
│    → Update incident: triage_json, remediation_json, validation_json,       │
│      proposed_patch, status (blocked | awaiting_approval | approved)         │
│    → Upsert evidence + triage into Qdrant                                  │
│    → Return triage, remediation, validation, proposed_patch, status         │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
          ┌────────────────────────────┼────────────────────────────┐
          ▼                            ▼                            ▼
┌──────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│ 3a. APPROVAL     │    │ 3b. (Optional) PR     │    │ 3c. (Optional) Slack │
│ POST /approvals  │    │ POST /incidents/{id}/pr│   │ (not auto-triggered)  │
│ → Stores approver│    │ → Branch, commit patch │   │ POST /webhooks/slack  │
│   decision,      │    │   open PR, store in   │   │ → Approve/Reject       │
│   updates        │    │   incident_pr         │   │   updates approval    │
│   incident status│    │ GET /incidents/{id}/pr │   │   in DB               │
└──────────────────┘    └──────────────────────┘    └──────────────────────┘
```

**Optional reads:**
- `GET /lineage?model_id=&run_id=` — upstream/downstream for a model.
- `GET /lineage/blast-radius?model_id=&run_id=&max_depth=` — impact analysis.
- `GET /incidents/{id}/pr` — PR status for an incident.

---

## 5. Tech Components by Phase

### Phase 1: Ingest
| Component | Role |
|-----------|------|
| **FastAPI** | `POST /ingest/dbt_run` handler |
| **Pydantic** | `DbtRunIngestRequest` / `DbtRunIngestResponse` |
| **Postgres (psycopg)** | `upsert_pipeline`, `insert_pipeline_run`, `create_incident`, `insert_audit_event` |
| **Hash (md5)** | `incident_id` from run_id, `pipeline_id` from name+env |

### Phase 2: Agent Run
| Component | Role |
|-----------|------|
| **FastAPI** | `POST /agent/run` handler |
| **Postgres** | `get_run_by_incident` (manifest + run_results) |
| **Agent (app/agent.py)** | `run_agent_loop`: heuristic path or `_run_llm_agent_loop` |
| **Anthropic (Claude)** | Messages API with tools, tool_use handling (when not MOCK_LLM) |
| **app/tools.py** | `TOOL_SCHEMAS`, `execute_tool` → retrieve_evidence, query_lineage, search_similar_incidents, propose_patch |
| **app/lineage.py** | `LineageGraph(manifest)` — BFS, blast_radius |
| **Qdrant (vector_store)** | `search_similar_incidents`; after run: `upsert_evidence`, `upsert_triage_result` |
| **app/dbt_validator.py** | Optional sandboxed `dbt compile` / `dbt test` when `DBT_PROJECT_DIR` set |
| **Postgres** | `update_incident_agent_output`, `insert_audit_event` |

### Phase 3a: Approvals
| Component | Role |
|-----------|------|
| **FastAPI** | `POST /approvals` |
| **Postgres** | `add_approval` (insert approval row, update incident status) |

### Phase 3b: GitHub PR
| Component | Role |
|-----------|------|
| **FastAPI** | `POST /incidents/{id}/pr`, `GET /incidents/{id}/pr` |
| **Postgres** | `get_run_by_incident`, `insert_incident_pr`, `get_incident_pr` |
| **httpx** | Async GitHub API: refs (base SHA), create branch, Contents API (commit file), create PR, check runs |
| **app/github.py** | `create_pr_for_incident`, `format_pr_body` |

### Phase 3c: Slack
| Component | Role |
|-----------|------|
| **FastAPI** | `POST /webhooks/slack` (interactive payload) |
| **app/slack.py** | `handle_slack_interaction` (approve/reject → `add_approval`), `post_incident_notification` (Block Kit, chat.postMessage) |
| **httpx** | `chat.postMessage` with blocks |
| **Postgres** | `insert_notification`, `insert_audit_event` |

### Phase: Lineage (read-only)
| Component | Role |
|-----------|------|
| **FastAPI** | `GET /lineage`, `GET /lineage/blast-radius` |
| **Postgres** | `get_manifest_by_run_id` |
| **networkx** | Directed graph, BFS |
| **app/lineage.py** | `LineageGraph`, `to_serializable`, `blast_radius` |

### Phase: Eval
| Component | Role |
|-----------|------|
| **eval/score.py** | `predict_cause`, `precision_recall_f1`, `patch_quality_score`, `blast_radius_accuracy`; modes: quick, full, regression |
| **httpx** (full mode) | POST ingest, POST agent/run, then score |
| **eval/incidents.jsonl** | Input rows with message, expected_cause, run_results, manifest, optional expected_patch_contains / expected_blast_radius_min |

### Shared / Config
| Component | Role |
|-----------|------|
| **app/config.py** | `Settings` from env (DB, Qdrant, Anthropic, OpenAI, GitHub, Slack, dbt) |
| **Docker Compose** | Postgres 16, Qdrant; `.env` for secrets and flags |
| **OpenAI** (optional) | Embeddings in VectorStore when `OPENAI_API_KEY` set |

---

## 6. File Map (Quick Reference)

| Path | Purpose |
|------|---------|
| `app/main.py` | FastAPI app, routes: health, ingest, agent/run, approvals, lineage, lineage/blast-radius, incidents/{id}/pr, webhooks/slack |
| `app/agent.py` | Evidence retrieval, heuristic triage/remediation, validate_patch, LLM ReAct loop, output parsing |
| `app/tools.py` | Tool schemas and execute_tool (evidence, lineage, similar incidents, propose_patch) |
| `app/db.py` | Postgres schema and all DB operations |
| `app/vector_store.py` | Qdrant client, embeddings (OpenAI or hash), upsert/search |
| `app/lineage.py` | LineageGraph from manifest, BFS, blast_radius, to_serializable |
| `app/models.py` | Pydantic request/response models |
| `app/config.py` | Settings dataclass from env |
| `app/dbt_validator.py` | Sandboxed dbt compile/test for patch validation |
| `app/github.py` | GitHub branch, commit, PR, PR status, PR body formatting |
| `app/slack.py` | Block Kit incident message, post_incident_notification, handle_slack_interaction |
| `eval/score.py` | predict_cause, metrics, quick/full/regression modes |
| `eval/incidents.jsonl` | 20 eval cases |
| `scripts/demo.sh` | Ingest → agent run → approval |
| `scripts/sample_ingest.json` | Sample failed run payload |
| `docker-compose.yml` | Postgres 16, Qdrant |
| `.env.example` | Env template for all integrations |

---

*Generated from codebase review. For runbook and exact env vars, see README.md and .env.example.*
