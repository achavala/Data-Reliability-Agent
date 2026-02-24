from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DbtRunIngestRequest(BaseModel):
    pipeline_name: str
    environment: str = "prod"
    run_id: str
    status: str
    owner: str | None = None
    manifest: dict[str, Any]
    run_results: dict[str, Any]


class DbtRunIngestResponse(BaseModel):
    run_id: str
    incident_id: str | None
    created: bool


class AgentRunRequest(BaseModel):
    incident_id: str
    approval_required: bool = True


class AgentRunResponse(BaseModel):
    incident_id: str
    triage: dict[str, Any]
    remediation: dict[str, Any]
    validation: dict[str, Any]
    proposed_patch: str
    requires_human_approval: bool
    status: str


class ApprovalRequest(BaseModel):
    incident_id: str
    approver: str
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str | None = None


class ApprovalResponse(BaseModel):
    incident_id: str
    status: str
    updated_at: datetime


# M6: Lineage
class LineageNode(BaseModel):
    unique_id: str
    resource_type: str
    name: str
    depth: int | None = None


class LineageResponse(BaseModel):
    node_id: str
    upstream: list[LineageNode]
    downstream: list[LineageNode]


class BlastRadiusResponse(BaseModel):
    node_id: str
    impacted_model_count: int
    impacted_nodes: list[LineageNode]
    impacted_exposures: list[str]
    impacted_metrics: list[str]
    max_depth: int


# M3: GitHub PR
class PRStatusResponse(BaseModel):
    incident_id: str
    pr_number: int
    pr_url: str
    branch_name: str
    status: str
