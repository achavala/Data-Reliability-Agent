# Data Reliability Agent — System Flow Diagram

End-to-end flow with all tools and technologies. Diagrams use [Mermaid](https://mermaid.js.org/) (renders in GitHub, VS Code, and many Markdown viewers).

---

## How to view in Cursor (Mermaid + Markdown preview)

1. **Install a Mermaid preview extension**
   - Press **`Cmd+Shift+X`** (macOS) or **`Ctrl+Shift+X`** (Windows/Linux) to open the **Extensions** sidebar.
   - Search for **`Mermaid`**.
   - Install one of:
     - **Markdown Preview Mermaid Support** (e.g. by **Matt Bierner**), or  
     - **Mermaid Editor** (e.g. by **Tomoyuki Kimura**), or  
     - **Mermaid** (by **Mermaid**).
   - Reload the window if prompted.

2. **Open Markdown preview**
   - With `SYSTEM_FLOW_DIAGRAM.md` open, press **`Cmd+Shift+V`** (macOS) or **`Ctrl+Shift+V`** (Windows/Linux) for **Markdown: Open Preview**, or  
   - Press **`Cmd+K V`** / **`Ctrl+K V`** for **Markdown: Open Preview to the Side** (editor + preview side by side).

3. **Result**
   - The preview will render the Mermaid code blocks as diagrams. Use side-by-side view to scroll the doc and see each diagram.

*If diagrams don’t render, ensure the extension supports Mermaid in Markdown preview; “Markdown Preview Mermaid Support” is the usual choice.*

---

## 1. High-Level System Flow (All Phases)

```mermaid
flowchart TB
    subgraph external["External Triggers"]
        CI["CI / Orchestrator<br/>(dbt Cloud, Airflow, Dagster)"]
        Human["Human (API client)"]
        SlackUI["Slack (Approve/Reject)"]
    end

    subgraph api["API Layer"]
        FastAPI["FastAPI<br/>Python 3.11+"]
    end

    subgraph phase1["Phase 1: Ingest"]
        P1_Ingest["POST /ingest/dbt_run"]
        Pydantic1["Pydantic (request/response)"]
        Hash["hashlib (md5)"]
        P1_Ingest --> Pydantic1
        P1_Ingest --> Hash
    end

    subgraph phase2["Phase 2: Agent Run"]
        P2_Agent["POST /agent/run"]
        AgentPy["app/agent.py"]
        Tools["app/tools.py<br/>(TOOL_SCHEMAS, execute_tool)"]
        LineagePy["app/lineage.py<br/>(LineageGraph)"]
        DbtVal["app/dbt_validator.py"]
        P2_Agent --> AgentPy
        AgentPy --> Tools
        Tools --> LineagePy
        AgentPy --> DbtVal
    end

    subgraph phase3["Phase 3: Approval & Integrations"]
        P3_Approval["POST /approvals"]
        P3_PR["POST /incidents/{id}/pr"]
        P3_Slack["POST /webhooks/slack"]
        GitHubMod["app/github.py"]
        SlackMod["app/slack.py"]
        P3_PR --> GitHubMod
        P3_Slack --> SlackMod
    end

    subgraph persistence["Persistence"]
        Postgres["PostgreSQL 16<br/>(psycopg)"]
        Qdrant["Qdrant<br/>(qdrant-client)"]
    end

    subgraph llm["LLM & Embeddings (optional)"]
        Anthropic["Anthropic API<br/>(Claude)"]
        OpenAI["OpenAI API<br/>(embeddings)"]
    end

    subgraph validation["Validation (optional)"]
        DbtCLI["dbt CLI<br/>(compile, test)"]
    end

    subgraph externalsvc["External Services"]
        GitHubAPI["GitHub API<br/>(httpx)"]
        SlackAPI["Slack API<br/>(httpx)"]
    end

    CI --> FastAPI
    Human --> FastAPI
    SlackUI --> FastAPI

    FastAPI --> P1_Ingest
    FastAPI --> P2_Agent
    FastAPI --> P3_Approval
    FastAPI --> P3_PR
    FastAPI --> P3_Slack

    P1_Ingest --> Postgres
    AgentPy --> Anthropic
    Tools --> Qdrant
    Tools --> LineagePy
    LineagePy --> Postgres
    DbtVal --> DbtCLI
    AgentPy --> Postgres
    Qdrant --> OpenAI
    GitHubMod --> GitHubAPI
    SlackMod --> SlackAPI
    P3_Approval --> Postgres
    P3_PR --> Postgres
    SlackMod --> Postgres
```

---

## 2. Detailed Agent Loop (Tools & Technologies)

```mermaid
flowchart LR
    subgraph request["Request"]
        Req["POST /agent/run<br/>{ incident_id, approval_required }"]
    end

    subgraph fastapi["FastAPI"]
        Handler["main.py: agent_run()"]
    end

    subgraph db_read["Postgres (read)"]
        GetRun["db.get_run_by_incident()<br/>manifest_json, run_results_json"]
    end

    subgraph agent_loop["Agent Loop (app/agent.py)"]
        direction TB
        CheckMode{"anthropic_api_key<br/>&& !mock_llm?"}
        Heuristic["Heuristic path<br/>retrieve_evidence → triage →<br/>propose_remediation → validate_patch"]
        LLMLoop["LLM ReAct loop<br/>_run_llm_agent_loop()"]
        CheckMode -->|Yes| LLMLoop
        CheckMode -->|No| Heuristic

        subgraph tools["Tools (app/tools.py)"]
            T1["retrieve_evidence<br/>(agent.retrieve_evidence)"]
            T2["query_lineage<br/>(LineageGraph, networkx)"]
            T3["search_similar_incidents<br/>(Qdrant + OpenAI/hash embed)"]
            T4["propose_patch<br/>(returns patch_sql)"]
        end

        LLMLoop --> T1
        LLMLoop --> T2
        LLMLoop --> T3
        LLMLoop --> T4
        Heuristic --> T1
    end

    subgraph lineage["Lineage (app/lineage.py)"]
        NX["networkx.DiGraph"]
        BFS["BFS (upstream/downstream)"]
        BR["blast_radius()"]
        NX --> BFS
        NX --> BR
    end

    subgraph vector["Vector Store (app/vector_store.py)"]
        QClient["QdrantClient"]
        Embed["OpenAI embeddings or<br/>hashlib.sha256 (deterministic)"]
        Upsert["upsert_evidence, upsert_triage_result"]
        Search["search_similar_incidents"]
        QClient --> Embed
        QClient --> Upsert
        QClient --> Search
    end

    subgraph validation_block["Patch Validation"]
        Static["Static safety checks<br/>(re: DROP/TRUNCATE/DELETE)"]
        DbtOpt["Optional: dbt_validator<br/>tempdir → copy project →<br/>dbt compile --select<br/>dbt test --select"]
        Static --> DbtOpt
    end

    subgraph db_write["Postgres (write)"]
        UpdateIncident["db.update_incident_agent_output()"]
        Audit["db.insert_audit_event()"]
    end

    subgraph qdrant_write["Qdrant (write)"]
        VecUpsert["vector_store.upsert_evidence()<br/>vector_store.upsert_triage_result()"]
    end

    Req --> Handler
    Handler --> GetRun
    GetRun --> agent_loop
    T2 --> lineage
    T3 --> vector
    agent_loop --> validation_block
    validation_block --> UpdateIncident
    agent_loop --> Audit
    agent_loop --> VecUpsert
```

---

## 3. Data Flow & Technology per Step

```mermaid
flowchart TB
    subgraph step1["1. INGEST"]
        S1A["Client sends manifest + run_results"]
        S1B["FastAPI: POST /ingest/dbt_run"]
        S1C["Pydantic: DbtRunIngestRequest"]
        S1D["hashlib.md5: pipeline_id, incident_id"]
        S1E["Postgres (psycopg): upsert_pipeline,<br/>insert_pipeline_run, create_incident,<br/>insert_audit_event"]
        S1A --> S1B --> S1C --> S1D --> S1E
    end

    subgraph step2["2. AGENT RUN"]
        S2A["FastAPI: POST /agent/run"]
        S2B["Postgres: get_run_by_incident"]
        S2C["Anthropic (Claude) or heuristic"]
        S2D["Tools: retrieve_evidence, query_lineage,<br/>search_similar_incidents, propose_patch"]
        S2E["networkx: LineageGraph BFS/blast_radius"]
        S2F["Qdrant: search; OpenAI or hashlib: embed"]
        S2G["dbt_validator: subprocess dbt compile/test<br/>(if DBT_PROJECT_DIR set)"]
        S2H["Postgres: update_incident_agent_output,<br/>insert_audit_event"]
        S2I["Qdrant: upsert_evidence, upsert_triage_result"]
        S2A --> S2B --> S2C --> S2D
        S2D --> S2E
        S2D --> S2F
        S2C --> S2G --> S2H --> S2I
    end

    subgraph step3a["3a. APPROVALS"]
        S3A1["FastAPI: POST /approvals"]
        S3A2["Pydantic: ApprovalRequest"]
        S3A3["Postgres: add_approval, update fact_incident"]
        S3A1 --> S3A2 --> S3A3
    end

    subgraph step3b["3b. GITHUB PR"]
        S3B1["FastAPI: POST /incidents/{id}/pr"]
        S3B2["app/github.py: create_pr_for_incident"]
        S3B3["httpx: GitHub API (refs, create branch,<br/>Contents API commit, create PR)"]
        S3B4["Postgres: insert_incident_pr"]
        S3B1 --> S3B2 --> S3B3 --> S3B4
    end

    subgraph step3c["3c. SLACK"]
        S3C1["Slack → POST /webhooks/slack"]
        S3C2["app/slack.py: handle_slack_interaction"]
        S3C3["Postgres: add_approval, insert_audit_event"]
        S3C1 --> S3C2 --> S3C3
    end

    subgraph lineage_api["LINEAGE (read-only)"]
        L1["GET /lineage?model_id=&run_id="]
        L2["GET /lineage/blast-radius?model_id=&run_id="]
        L3["Postgres: get_manifest_by_run_id"]
        L4["LineageGraph (networkx), to_serializable, blast_radius"]
        L1 --> L3 --> L4
        L2 --> L3 --> L4
    end

    step1 --> step2
    step2 --> step3a
    step2 --> step3b
    step2 --> step3c
```

---

## 4. Technology Stack Overview

```mermaid
flowchart LR
    subgraph runtime["Runtime & API"]
        Python["Python 3.11+"]
        FastAPI2["FastAPI"]
        Uvicorn["uvicorn"]
        Pydantic2["Pydantic"]
    end

    subgraph data["Data Stores"]
        Postgres2["PostgreSQL 16"]
        Qdrant2["Qdrant"]
        Psycopg["psycopg"]
        QdrantClient["qdrant-client"]
    end

    subgraph agent_stack["Agent & Logic"]
        Anthropic2["Anthropic (Claude)"]
        Networkx["networkx"]
        AppAgent["app/agent.py"]
        AppTools["app/tools.py"]
        AppLineage["app/lineage.py"]
        AppDbt["app/dbt_validator.py"]
    end

    subgraph embeddings["Embeddings"]
        OpenAI2["OpenAI API"]
        Hashlib["hashlib (fallback)"]
    end

    subgraph integrations["Integrations"]
        Httpx["httpx"]
        GitHub2["GitHub API"]
        Slack2["Slack API"]
    end

    subgraph validation_stack["Validation"]
        DbtCLI2["dbt CLI"]
        Re["re (safety patterns)"]
    end

    subgraph infra["Infrastructure"]
        Docker["Docker Compose"]
        Env[".env / python-dotenv"]
    end

    subgraph eval_stack["Eval"]
        EvalScore["eval/score.py"]
        EvalType["eval_type_backport"]
        IncidentsJSONL["eval/incidents.jsonl"]
    end

    Python --> FastAPI2
    FastAPI2 --> Uvicorn
    FastAPI2 --> Pydantic2
    Psycopg --> Postgres2
    QdrantClient --> Qdrant2
    AppAgent --> Anthropic2
    AppLineage --> Networkx
    AppDbt --> DbtCLI2
    OpenAI2 --> QdrantClient
    Httpx --> GitHub2
    Httpx --> Slack2
    Docker --> Postgres2
    Docker --> Qdrant2
```

---

## 5. Technology Legend (All Components)

| Technology | Where used | Purpose |
|------------|------------|---------|
| **Python 3.11+** | Entire app | Runtime |
| **FastAPI** | `app/main.py` | HTTP API, routes |
| **uvicorn** | Run command | ASGI server |
| **Pydantic** | `app/models.py`, request/response | Validation, serialization |
| **PostgreSQL 16** | `app/db.py`, Docker | Persistence (pipelines, runs, incidents, audit, approvals, PRs, notifications, traces) |
| **psycopg** | `app/db.py` | Postgres driver |
| **Qdrant** | `app/vector_store.py`, Docker | Vector store for evidence/triage similarity search |
| **qdrant-client** | `app/vector_store.py` | Qdrant Python client |
| **Anthropic (Claude)** | `app/agent.py` | LLM for ReAct agent loop (when not MOCK_LLM) |
| **OpenAI API** | `app/vector_store.py` | Embeddings (when OPENAI_API_KEY set) |
| **hashlib** | `app/vector_store.py`, `app/main.py` | Deterministic embeddings; pipeline_id, incident_id hashes |
| **networkx** | `app/lineage.py` | Directed graph, BFS for lineage/blast radius |
| **httpx** | `app/github.py`, `app/slack.py`, eval full mode | Async HTTP (GitHub, Slack, eval API calls) |
| **dbt CLI** | `app/dbt_validator.py` | `dbt compile`, `dbt test` in sandbox (when DBT_PROJECT_DIR set) |
| **re** | `app/agent.py` | Safety regex (DROP/TRUNCATE/DELETE) |
| **Docker Compose** | `docker-compose.yml` | Postgres 16, Qdrant services |
| **python-dotenv** | `app/config.py` | Load `.env` |
| **eval_type_backport** | `eval/score.py` | Eval harness dependency |
| **JSON** | Throughout | manifest, run_results, audit payloads |

---

*To view Mermaid diagrams in Cursor: see **How to view in Cursor** at the top of this file. Alternatively use GitHub, or [mermaid.live](https://mermaid.live).*
