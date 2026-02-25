from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.agent import run_agent_loop
from app.config import settings
from app.db import (
    add_approval,
    count_incidents,
    create_incident,
    get_approvals_for_incident,
    get_audit_events_for_incident,
    get_incident_detail,
    get_incident_pr,
    get_manifest_by_run_id,
    get_run_by_incident,
    init_db,
    insert_audit_event,
    insert_incident_pr,
    insert_notification,
    insert_pipeline_run,
    list_incidents,
    update_incident_agent_output,
    upsert_pipeline,
)
from app.github import create_pr_for_incident, get_pr_status
from app.lineage import LineageGraph
from app.models import (
    AgentRunRequest,
    AgentRunResponse,
    ApprovalItemResponse,
    ApprovalRequest,
    ApprovalResponse,
    AuditEventResponse,
    BlastRadiusResponse,
    DbtRunIngestRequest,
    DbtRunIngestResponse,
    IncidentDetailResponse,
    IncidentListResponse,
    IncidentSummary,
    LineageNode,
    LineageResponse,
    PRStatusResponse,
)
from app.slack import handle_slack_interaction, post_incident_notification
from app.vector_store import VectorStore


app = FastAPI(title="Data Reliability Agent", version="0.2.0")
vector_store = VectorStore()
READONLY_VIEW_PATH = Path(__file__).resolve().parent / "static" / "readonly.html"


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Read-only guard
# ---------------------------------------------------------------------------


def _is_local_client(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("::ffff:"):
        normalized = normalized.split("::ffff:", 1)[1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _enforce_readonly_guard(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if not settings.readonly_allow_remote and not _is_local_client(client_host):
        raise HTTPException(status_code=403, detail="Read-only viewer is restricted to localhost")

    if settings.readonly_view_token:
        token = request.headers.get("X-Viewer-Token") or request.query_params.get("token")
        if token != settings.readonly_view_token:
            raise HTTPException(status_code=401, detail="Invalid read-only viewer token")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Read-only APIs + browser view
# ---------------------------------------------------------------------------


@app.get("/readonly", response_class=HTMLResponse)
def readonly_view(request: Request) -> str:
    _enforce_readonly_guard(request)
    return READONLY_VIEW_PATH.read_text(encoding="utf-8")


@app.get("/api/readonly/incidents", response_model=IncidentListResponse)
def readonly_incident_list(
    request: Request,
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None),
) -> IncidentListResponse:
    _enforce_readonly_guard(request)
    rows = list_incidents(limit=limit, offset=offset, status=status)
    total = count_incidents(status=status)
    return IncidentListResponse(
        total=total,
        limit=limit,
        offset=offset,
        incidents=[IncidentSummary(**row) for row in rows],
    )


@app.get("/api/readonly/incidents/{incident_id}", response_model=IncidentDetailResponse)
def readonly_incident_detail(request: Request, incident_id: str) -> IncidentDetailResponse:
    _enforce_readonly_guard(request)
    row = get_incident_detail(incident_id)
    if not row:
        raise HTTPException(status_code=404, detail="Incident not found")

    return IncidentDetailResponse(
        incident_id=row["incident_id"],
        run_id=row["run_id"],
        severity=row["severity"],
        status=row["status"],
        pipeline_name=row["pipeline_name"],
        environment=row["environment"],
        owner=row.get("owner"),
        run_status=row["run_status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        triage=row.get("triage_json"),
        remediation=row.get("remediation_json"),
        validation=row.get("validation_json"),
        proposed_patch=row.get("proposed_patch"),
        requires_human_approval=row["requires_human_approval"],
        pr_number=row.get("pr_number"),
        pr_url=row.get("pr_url"),
        pr_status=row.get("pr_status"),
    )


@app.get("/api/readonly/incidents/{incident_id}/events", response_model=list[AuditEventResponse])
def readonly_incident_events(
    request: Request,
    incident_id: str,
    limit: int = Query(100, ge=1, le=500),
) -> list[AuditEventResponse]:
    _enforce_readonly_guard(request)
    row = get_run_by_incident(incident_id)
    if not row:
        raise HTTPException(status_code=404, detail="Incident not found")

    events = get_audit_events_for_incident(incident_id=incident_id, limit=limit)
    return [AuditEventResponse(**event) for event in events]


@app.get("/api/readonly/incidents/{incident_id}/approvals", response_model=list[ApprovalItemResponse])
def readonly_incident_approvals(request: Request, incident_id: str) -> list[ApprovalItemResponse]:
    _enforce_readonly_guard(request)
    row = get_run_by_incident(incident_id)
    if not row:
        raise HTTPException(status_code=404, detail="Incident not found")

    approvals = get_approvals_for_incident(incident_id=incident_id)
    return [ApprovalItemResponse(**approval) for approval in approvals]


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
async def agent_run(payload: AgentRunRequest) -> AgentRunResponse:
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

    # --- Orchestration: auto-create PR and notify Slack ---
    pr_url = None
    if status != "blocked" and patch:
        model_path = f"models/{payload.incident_id}.sql"
        pr_result = await create_pr_for_incident(
            payload.incident_id, model_path, patch, triage_result, remediation, validation
        )
        if "error" not in pr_result:
            insert_incident_pr(
                payload.incident_id,
                pr_result["pr_number"],
                pr_result["pr_url"],
                pr_result["branch_name"],
            )
            pr_url = pr_result["pr_url"]

    if status != "blocked":
        slack_result = await post_incident_notification(
            payload.incident_id, triage_result, remediation, validation, pr_url=pr_url
        )
        if slack_result:
            insert_notification(
                payload.incident_id,
                slack_result["channel"],
                slack_result.get("message_ts"),
                "agent_run_completed",
            )

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
async def approvals(payload: ApprovalRequest) -> ApprovalResponse:
    run_row = get_run_by_incident(payload.incident_id)
    if not run_row:
        raise HTTPException(status_code=404, detail="Incident not found")

    updated_at = add_approval(payload.incident_id, payload.approver, payload.decision, payload.comment)
    insert_audit_event(
        payload.incident_id,
        "approval_recorded",
        {"approver": payload.approver, "decision": payload.decision, "comment": payload.comment},
    )

    # --- Orchestration: create PR on approval if not already created ---
    if payload.decision == "approve":
        existing_pr = get_incident_pr(payload.incident_id)
        if not existing_pr:
            triage = run_row.get("triage_json") or {}
            remediation = run_row.get("remediation_json") or {}
            validation = run_row.get("validation_json") or {}
            patch = run_row.get("proposed_patch") or ""
            if patch:
                model_path = f"models/{payload.incident_id}.sql"
                pr_result = await create_pr_for_incident(
                    payload.incident_id, model_path, patch, triage, remediation, validation,
                )
                if "error" not in pr_result:
                    insert_incident_pr(
                        payload.incident_id,
                        pr_result["pr_number"],
                        pr_result["pr_url"],
                        pr_result["branch_name"],
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
