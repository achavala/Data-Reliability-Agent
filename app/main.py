from __future__ import annotations

import hashlib
import json

from fastapi import FastAPI, HTTPException, Query, Request

from app.agent import run_agent_loop
from app.db import (
    add_approval,
    create_incident,
    get_incident_pr,
    get_manifest_by_run_id,
    get_run_by_incident,
    init_db,
    insert_audit_event,
    insert_incident_pr,
    insert_notification,
    insert_pipeline_run,
    update_incident_agent_output,
    upsert_pipeline,
)
from app.github import create_pr_for_incident, get_pr_status
from app.lineage import LineageGraph
from app.models import (
    AgentRunRequest,
    AgentRunResponse,
    ApprovalRequest,
    ApprovalResponse,
    BlastRadiusResponse,
    DbtRunIngestRequest,
    DbtRunIngestResponse,
    LineageNode,
    LineageResponse,
    PRStatusResponse,
)
from app.slack import handle_slack_interaction, post_incident_notification
from app.vector_store import VectorStore


app = FastAPI(title="Data Reliability Agent", version="0.2.0")
vector_store = VectorStore()


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


@app.post("/ingest/dbt_run", response_model=DbtRunIngestResponse)
def ingest_dbt_run(payload: DbtRunIngestRequest) -> DbtRunIngestResponse:
    pipeline_id = hashlib.md5(f"{payload.pipeline_name}:{payload.environment}".encode("utf-8")).hexdigest()
    upsert_pipeline(pipeline_id, payload.pipeline_name, payload.owner, payload.environment)
    insert_pipeline_run(payload.run_id, pipeline_id, payload.status, payload.run_results, payload.manifest)

    incident_id = None
    created = False
    if payload.status.lower() in {"error", "failed", "fail"}:
        incident_id = hashlib.md5(payload.run_id.encode("utf-8")).hexdigest()
        create_incident(incident_id=incident_id, run_id=payload.run_id)
        insert_audit_event(incident_id, "incident_created", {"run_id": payload.run_id})
        created = True

    return DbtRunIngestResponse(run_id=payload.run_id, incident_id=incident_id, created=created)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


@app.post("/agent/run", response_model=AgentRunResponse)
def agent_run(payload: AgentRunRequest) -> AgentRunResponse:
    run_row = get_run_by_incident(payload.incident_id)
    if not run_row:
        raise HTTPException(status_code=404, detail="Incident not found")

    triage_result, remediation, validation, patch = run_agent_loop(
        payload.incident_id, run_row, vector_store=vector_store
    )
    vector_store.upsert_evidence(payload.incident_id, {"triage": triage_result, "remediation": remediation})
    vector_store.upsert_triage_result(payload.incident_id, triage_result)

    requires_human_approval = payload.approval_required
    if validation["dbt_compile"] != "pass" or validation["dbt_test"] != "pass" or validation["safety_checks"] != "pass":
        status = "blocked"
    else:
        status = "awaiting_approval" if requires_human_approval else "approved"

    update_incident_agent_output(
        payload.incident_id,
        triage_result,
        remediation,
        validation,
        patch,
        requires_human_approval,
        status,
    )

    insert_audit_event(payload.incident_id, "agent_run_completed", {"status": status})

    return AgentRunResponse(
        incident_id=payload.incident_id,
        triage=triage_result,
        remediation=remediation,
        validation=validation,
        proposed_patch=patch,
        requires_human_approval=requires_human_approval,
        status=status,
    )


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@app.post("/approvals", response_model=ApprovalResponse)
def approvals(payload: ApprovalRequest) -> ApprovalResponse:
    run_row = get_run_by_incident(payload.incident_id)
    if not run_row:
        raise HTTPException(status_code=404, detail="Incident not found")

    updated_at = add_approval(payload.incident_id, payload.approver, payload.decision, payload.comment)
    insert_audit_event(
        payload.incident_id,
        "approval_recorded",
        {"approver": payload.approver, "decision": payload.decision, "comment": payload.comment},
    )

    status = "approved" if payload.decision == "approve" else "rejected"
    return ApprovalResponse(incident_id=payload.incident_id, status=status, updated_at=updated_at)


# ---------------------------------------------------------------------------
# M6: Lineage
# ---------------------------------------------------------------------------


@app.get("/lineage", response_model=LineageResponse)
def get_lineage(
    model_id: str = Query(..., description="dbt unique_id (e.g. model.analytics.orders)"),
    run_id: str = Query(..., description="Pipeline run ID to load manifest from"),
):
    manifest = get_manifest_by_run_id(run_id)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"No manifest found for run_id {run_id}")

    graph = LineageGraph(manifest)
    data = graph.to_serializable(model_id)

    return LineageResponse(
        node_id=model_id,
        upstream=[LineageNode(**n) for n in data.get("upstream", [])],
        downstream=[LineageNode(**n) for n in data.get("downstream", [])],
    )


@app.get("/lineage/blast-radius", response_model=BlastRadiusResponse)
def get_blast_radius(
    model_id: str = Query(..., description="dbt unique_id (e.g. model.analytics.orders)"),
    run_id: str = Query(..., description="Pipeline run ID to load manifest from"),
    max_depth: int = Query(10, description="Maximum traversal depth"),
):
    manifest = get_manifest_by_run_id(run_id)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"No manifest found for run_id {run_id}")

    graph = LineageGraph(manifest)
    result = graph.blast_radius(model_id, max_depth=max_depth)

    return BlastRadiusResponse(
        node_id=model_id,
        impacted_model_count=result["impacted_model_count"],
        impacted_nodes=[LineageNode(**n) for n in result["impacted_nodes"]],
        impacted_exposures=result["impacted_exposures"],
        impacted_metrics=result["impacted_metrics"],
        max_depth=result["max_depth"],
    )


# ---------------------------------------------------------------------------
# M3: GitHub PR
# ---------------------------------------------------------------------------


@app.get("/incidents/{incident_id}/pr", response_model=PRStatusResponse)
async def get_incident_pr_status(incident_id: str):
    pr = get_incident_pr(incident_id)
    if not pr:
        raise HTTPException(status_code=404, detail="No PR found for this incident")

    return PRStatusResponse(
        incident_id=incident_id,
        pr_number=pr["github_pr_number"],
        pr_url=pr["github_pr_url"],
        branch_name=pr["branch_name"],
        status=pr["status"],
    )


@app.post("/incidents/{incident_id}/pr", response_model=PRStatusResponse)
async def create_incident_pr_endpoint(incident_id: str):
    run_row = get_run_by_incident(incident_id)
    if not run_row:
        raise HTTPException(status_code=404, detail="Incident not found")

    triage = run_row.get("triage_json") or {}
    remediation = run_row.get("remediation_json") or {}
    validation = run_row.get("validation_json") or {}
    patch = run_row.get("proposed_patch") or ""

    if not patch:
        raise HTTPException(status_code=400, detail="No patch available for this incident")

    # Derive model path from triage data
    failed_nodes = triage.get("root_cause_hypotheses", [])
    model_path = f"models/{incident_id}.sql"  # Default fallback

    pr_result = await create_pr_for_incident(
        incident_id, model_path, patch, triage, remediation, validation
    )

    if "error" in pr_result:
        raise HTTPException(status_code=500, detail=pr_result["error"])

    insert_incident_pr(
        incident_id,
        pr_result["pr_number"],
        pr_result["pr_url"],
        pr_result["branch_name"],
    )

    return PRStatusResponse(
        incident_id=incident_id,
        pr_number=pr_result["pr_number"],
        pr_url=pr_result["pr_url"],
        branch_name=pr_result["branch_name"],
        status=pr_result["status"],
    )


# ---------------------------------------------------------------------------
# M4: Slack
# ---------------------------------------------------------------------------


@app.post("/webhooks/slack")
async def slack_webhook(request: Request):
    """Handle Slack interactive message callbacks (approve/reject buttons)."""
    form_data = await request.form()
    payload = json.loads(form_data.get("payload", "{}"))
    result = await handle_slack_interaction(payload)
    return result
