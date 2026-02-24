from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from app.config import settings


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dim_pipeline (
    pipeline_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner TEXT,
    environment TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fact_pipeline_run (
    run_id TEXT PRIMARY KEY,
    pipeline_id TEXT NOT NULL REFERENCES dim_pipeline(pipeline_id),
    status TEXT NOT NULL,
    run_results_json JSONB NOT NULL,
    manifest_json JSONB NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dim_dataset_scd2 (
    row_id BIGSERIAL PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    name TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    is_current BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS fact_incident (
    incident_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES fact_pipeline_run(run_id),
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    triage_json JSONB,
    remediation_json JSONB,
    validation_json JSONB,
    proposed_patch TEXT,
    requires_human_approval BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_event (
    event_id BIGSERIAL PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES fact_incident(incident_id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id BIGSERIAL PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES fact_incident(incident_id),
    approver TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS incident_pr (
    pr_id BIGSERIAL PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES fact_incident(incident_id),
    github_pr_number INTEGER NOT NULL,
    github_pr_url TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS incident_notification (
    notification_id BIGSERIAL PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES fact_incident(incident_id),
    channel TEXT NOT NULL,
    message_ts TEXT,
    notification_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_trace (
    trace_id BIGSERIAL PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES fact_incident(incident_id),
    step_index INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    input_json JSONB NOT NULL,
    output_json JSONB NOT NULL,
    model_name TEXT,
    token_usage JSONB,
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        yield conn


def init_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()


def upsert_pipeline(pipeline_id: str, name: str, owner: str | None, environment: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dim_pipeline (pipeline_id, name, owner, environment)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (pipeline_id)
                DO UPDATE SET name = EXCLUDED.name, owner = EXCLUDED.owner, environment = EXCLUDED.environment
                """,
                (pipeline_id, name, owner, environment),
            )
        conn.commit()


def insert_pipeline_run(
    run_id: str,
    pipeline_id: str,
    status: str,
    run_results_json: dict[str, Any],
    manifest_json: dict[str, Any],
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fact_pipeline_run (run_id, pipeline_id, status, run_results_json, manifest_json)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (run_id) DO UPDATE
                SET status = EXCLUDED.status,
                    run_results_json = EXCLUDED.run_results_json,
                    manifest_json = EXCLUDED.manifest_json
                """,
                (run_id, pipeline_id, status, json.dumps(run_results_json), json.dumps(manifest_json)),
            )
        conn.commit()


def create_incident(incident_id: str, run_id: str, severity: str = "high", status: str = "open") -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fact_incident (incident_id, run_id, severity, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (incident_id) DO NOTHING
                """,
                (incident_id, run_id, severity, status),
            )
        conn.commit()


def get_run_by_incident(incident_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.incident_id, i.status AS incident_status, r.*
                FROM fact_incident i
                JOIN fact_pipeline_run r ON r.run_id = i.run_id
                WHERE i.incident_id = %s
                """,
                (incident_id,),
            )
            row = cur.fetchone()
            return row


def update_incident_agent_output(
    incident_id: str,
    triage: dict[str, Any],
    remediation: dict[str, Any],
    validation: dict[str, Any],
    proposed_patch: str,
    requires_human_approval: bool,
    status: str,
) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fact_incident
                SET triage_json = %s::jsonb,
                    remediation_json = %s::jsonb,
                    validation_json = %s::jsonb,
                    proposed_patch = %s,
                    requires_human_approval = %s,
                    status = %s,
                    updated_at = %s
                WHERE incident_id = %s
                """,
                (
                    json.dumps(triage),
                    json.dumps(remediation),
                    json.dumps(validation),
                    proposed_patch,
                    requires_human_approval,
                    status,
                    now,
                    incident_id,
                ),
            )
        conn.commit()


def insert_audit_event(incident_id: str, event_type: str, payload: dict[str, Any]) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_event (incident_id, event_type, payload)
                VALUES (%s, %s, %s::jsonb)
                """,
                (incident_id, event_type, json.dumps(payload)),
            )
        conn.commit()


def add_approval(incident_id: str, approver: str, decision: str, comment: str | None) -> datetime:
    now = datetime.now(timezone.utc)
    status = "approved" if decision == "approve" else "rejected"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approvals (incident_id, approver, decision, comment)
                VALUES (%s, %s, %s, %s)
                """,
                (incident_id, approver, decision, comment),
            )
            cur.execute(
                """
                UPDATE fact_incident
                SET status = %s, updated_at = %s
                WHERE incident_id = %s
                """,
                (status, now, incident_id),
            )
        conn.commit()
    return now


# ---------------------------------------------------------------------------
# M6: Lineage helpers
# ---------------------------------------------------------------------------


def get_manifest_by_run_id(run_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT manifest_json FROM fact_pipeline_run WHERE run_id = %s", (run_id,))
            row = cur.fetchone()
            return row["manifest_json"] if row else None


def get_dataset_schema_history(dataset_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM dim_dataset_scd2 WHERE dataset_id = %s ORDER BY valid_from DESC",
                (dataset_id,),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# M3: GitHub PR
# ---------------------------------------------------------------------------


def insert_incident_pr(
    incident_id: str, pr_number: int, pr_url: str, branch_name: str, status: str = "open"
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incident_pr (incident_id, github_pr_number, github_pr_url, branch_name, status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (incident_id, pr_number, pr_url, branch_name, status),
            )
        conn.commit()


def get_incident_pr(incident_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM incident_pr WHERE incident_id = %s ORDER BY created_at DESC LIMIT 1",
                (incident_id,),
            )
            return cur.fetchone()


def update_incident_pr_status(incident_id: str, status: str) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE incident_pr SET status = %s, updated_at = %s WHERE incident_id = %s",
                (status, now, incident_id),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# M4: Slack notifications
# ---------------------------------------------------------------------------


def insert_notification(
    incident_id: str, channel: str, message_ts: str | None, notification_type: str
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incident_notification (incident_id, channel, message_ts, notification_type)
                VALUES (%s, %s, %s, %s)
                """,
                (incident_id, channel, message_ts, notification_type),
            )
        conn.commit()


def get_notifications_for_incident(incident_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM incident_notification WHERE incident_id = %s ORDER BY created_at",
                (incident_id,),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# M7: Agent traces
# ---------------------------------------------------------------------------


def insert_agent_trace(
    incident_id: str,
    step_index: int,
    step_type: str,
    input_json: dict[str, Any],
    output_json: dict[str, Any],
    model_name: str | None = None,
    token_usage: dict[str, Any] | None = None,
    latency_ms: int | None = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_trace
                    (incident_id, step_index, step_type, input_json, output_json,
                     model_name, token_usage, latency_ms)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s)
                """,
                (
                    incident_id,
                    step_index,
                    step_type,
                    json.dumps(input_json, default=str),
                    json.dumps(output_json, default=str),
                    model_name,
                    json.dumps(token_usage) if token_usage else None,
                    latency_ms,
                ),
            )
        conn.commit()


def get_agent_traces(incident_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM agent_trace WHERE incident_id = %s ORDER BY step_index",
                (incident_id,),
            )
            return cur.fetchall()
