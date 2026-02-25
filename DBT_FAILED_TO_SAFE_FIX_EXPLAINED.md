# “dbt pipeline failed” → “safe, human-approved fix” — Explained in Detail

This document explains the main goal of the Data Reliability Agent (DRA): turning a **failed dbt pipeline run** into a **safe, human-approved fix**.

---

## Part 1: What “dbt pipeline failed” means

### 1.1 What is dbt?

**dbt (data build tool)** is the standard way many companies transform data in the warehouse: you write SQL (and a bit of config) to define **models** (tables/views). dbt runs these in a **DAG**: it builds staging models first, then analytics models that depend on them. Each run produces:

- A **manifest**: which models exist, their dependencies (parent/child), and metadata.
- **Run results**: for each node, status (`pass`, `error`, `fail`, etc.), error message, and sometimes compiled SQL.

So a **dbt pipeline** = one execution of that DAG (e.g. “build all models for prod”).

### 1.2 What does “failed” mean?

A run **fails** when at least one node does not succeed. Typical cases:

| Type | What happened | Example |
|------|----------------|--------|
| **Execution error** | The compiled SQL failed in the database. | “Column `order_total` does not exist” — the model expects a column that was removed or renamed upstream. |
| **Test failure** | A dbt test (e.g. `unique`, `not_null`) failed. | “Null value found in `order_id`” or “Duplicate rows in `order_items`.” |
| **Source freshness** | A freshness check on a source failed. | “Source `raw.orders` last loaded 48 hours ago; threshold is 24 hours.” |

When the run fails, you get a **run_id**, a **manifest**, and **run_results** with failed nodes and their **messages** and **compiled_code**. That’s the “dbt pipeline failed” state: **we know something broke, but we still have to figure out why and what to do.**

### 1.3 Concrete example (from the project)

From `scripts/sample_ingest.json`:

- **Pipeline:** `dbt-prod`, run `run_2026_02_24_001`, **status: failed**.
- **Failed node:** `model.analytics.orders`.
- **Message:** `"Database Error in model orders: column order_total does not exist"`.
- **Compiled SQL:** `select order_total from stg_orders`.

So: the model `orders` selects `order_total` from `stg_orders`, but that column no longer exists (e.g. renamed or dropped upstream). **“dbt pipeline failed”** here = **this run ended in error, and we have this evidence.**

Without DRA, an analyst would have to: read the error, open the model, check upstream schema, guess the right fix (e.g. use a new column or `coalesce`), edit SQL, run locally, then open a PR. DRA automates the **diagnosis and first-draft fix** so the human only **reviews and approves**.

---

## Part 2: What “safe, human-approved fix” means

### 2.1 What is the “fix”?

The fix is a **remediation** for the failure:

- **Root cause** (e.g. “upstream_schema_drift”, “transformation_logic_error”, “source_freshness_failure”).
- **Strategy and actions** (e.g. “Update model to map renamed columns”, “Add null check”).
- A **proposed patch**: **concrete SQL** (or model-level change) that, if applied, should resolve the failure.

So “fix” = **the patch plus the reasoning behind it**, not “we ran something in prod without review.”

### 2.2 Why “safe”?

The system enforces **safety** so the proposed change is not obviously dangerous:

1. **No destructive SQL**  
   The patch is checked for patterns like `DROP TABLE`, `TRUNCATE TABLE`, `DELETE FROM`. If any are found, **safety_checks = fail** and the incident can be marked **blocked** so no one is tempted to approve a dangerous change.

2. **Validation before approval**  
   - **dbt compile**: does the SQL (and refs) compile in the project?  
   - **dbt test**: do the tests for that model pass (when `DBT_PROJECT_DIR` is set)?  
   If compile or test fails, the incident stays **blocked**; the human sees validation results and can ask for a revised patch.

3. **No auto-apply**  
   The agent **never** applies the patch to the repo or the warehouse. It only **proposes**. So “safe” also means “we don’t change production or source code until a human explicitly approves.”

So **“safe”** = **no destructive SQL + validated (compile/test) + no automatic application.**

### 2.3 Why “human-approved”?

Even when validation passes, the **final decision** is a person’s:

- Someone (e.g. on-call, data lead) must **approve** or **reject** the remediation (via `POST /approvals` or Slack Approve/Reject).
- Only after **approve** does the incident status become **approved**; the actual “apply” step (e.g. creating a PR or merging) is a separate, human-driven or policy-driven step.
- Every approval/rejection is stored (e.g. in `approvals` and `audit_event`), so you have an **audit trail**: who approved what and when.

So **“human-approved”** = **no fix is considered final until a human has explicitly approved it, with full auditability.**

---

## Part 3: How DRA gets from “failed” to “safe, human-approved fix”

End-to-end, the journey looks like this.

### Step 1: Ingest the failure

- **Input:** CI/orchestrator (or you) send the failed run to DRA: **manifest + run_results** (e.g. `POST /ingest/dbt_run`).
- **What DRA does:** Stores pipeline and run in Postgres; if status is `error`/`failed`/`fail`, creates an **incident** and an audit event.
- **Output:** An **incident_id** tied to that run.

So “dbt pipeline failed” is now a **first-class incident** in DRA with full run context.

### Step 2: Run the agent (triage + remediation)

- **Input:** You call `POST /agent/run` with that **incident_id** (and e.g. `approval_required: true`).
- **What DRA does:**
  1. Loads the run from Postgres (manifest + run_results).
  2. Runs the **agent** (LLM or heuristic):
     - **Retrieve evidence:** Failed nodes, error messages, compiled SQL, lineage, schema-drift signals.
     - **Investigate:** Lineage/blast radius (which models/exposures are impacted), and optionally search for **similar past incidents** in the vector store.
     - **Triage:** Root-cause hypotheses with confidence and evidence.
     - **Remediate:** Propose a **patch** (strategy + actions + SQL) via the `propose_patch` tool.
  3. **Validates the patch:**
     - Static **safety checks** (no DROP/TRUNCATE/DELETE).
     - Optionally **dbt compile** and **dbt test** in a sandbox (if `DBT_PROJECT_DIR` is set).
  4. Updates the incident with: triage, remediation, validation result, proposed_patch, and status.
  5. Optionally indexes evidence and triage in Qdrant for future “similar incidents” search.

- **Output:**  
  - **Triage:** summary, root_cause_hypotheses, blast_radius.  
  - **Remediation:** strategy, actions, proposed_patch (SQL), risk.  
  - **Validation:** dbt_compile, dbt_test, safety_checks, (and violations if any).  
  - **Status:** `blocked` (if validation failed) or `awaiting_approval` (if validation passed and approval_required).

So we’ve gone from “run failed” to “here’s what we think happened, here’s the impact, here’s a concrete patch, and here’s whether it’s safe and compiles/tests.”

### Step 3: Human review and approval

- **Input:** A human (or a bot acting on behalf of a human) calls `POST /approvals` with **incident_id**, **approver**, **decision** (`approve` or `reject`), and optional **comment**. Alternatively, they click Approve/Reject in Slack (which hits `POST /webhooks/slack` and then records the approval the same way).
- **What DRA does:** Inserts into `approvals`, updates the incident **status** to `approved` or `rejected`, and writes an audit event.
- **Output:** The incident is now in a final approval state; downstream (e.g. “create PR”, “notify team”) can use this.

So **“human-approved”** is implemented literally: the fix is only considered approved after this step.

### Step 4 (optional): Turn the fix into a PR

- After approval, your process can call `POST /incidents/{id}/pr` (or you can do it before approval, depending on policy). DRA then uses **app/github.py** to create a branch, commit the **proposed_patch** to the right file, and open a PR with triage/remediation/validation in the body.
- The **actual merge** is still a human (or your merge rules) in GitHub. DRA only creates the PR; it doesn’t merge it.

So the **“fix”** becomes a **patch in a PR**, still under human control.

---

## Part 4: End-to-end in one picture

```
  "dbt pipeline failed"                    "safe, human-approved fix"
  ─────────────────────                   ─────────────────────────

  Run fails (e.g. column                   Same incident now has:
  order_total does not exist)              • Root cause + evidence
           │                               • Blast radius
           ▼                               • Proposed SQL patch
  POST /ingest/dbt_run                     • Validation: safety + dbt compile/test
  (manifest + run_results)                 • Status: awaiting_approval → approved
           │                               • Audit: who approved, when
           ▼
  Incident created (incident_id)
           │
           ▼
  POST /agent/run
  → Evidence + lineage + similar incidents
  → Triage (root cause, blast radius)
  → Propose patch (SQL)
  → Validate (no destructive SQL; dbt compile/test if configured)
  → Status: blocked or awaiting_approval
           │
           ▼
  Human reviews (API or Slack)
  POST /approvals { decision: "approve" }
           │
           ▼
  Status = approved
  (Optional: POST /incidents/{id}/pr → GitHub PR with patch)
```

---

## Part 5: Short summary

- **“dbt pipeline failed”** = a dbt run finished with at least one node in error/fail, and we have manifest + run_results (failed nodes, messages, compiled SQL).
- **“Safe, human-approved fix”** = a proposed remediation (root cause + concrete SQL patch) that:
  - is **safe**: no destructive SQL, and validated by dbt compile/test when configured,
  - is **human-approved**: a person (or delegated bot) has explicitly approved it, with full audit trail,
  - and is **not** auto-applied; applying it (e.g. via PR merge) is a separate, human-controlled step.

**DRA’s job** is to take the first state and produce the second: **automate diagnosis and first-draft fix**, while **keeping safety checks and human approval** so the fix is both correct and accountable.
