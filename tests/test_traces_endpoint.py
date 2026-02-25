"""Tests for GET /incidents/{incident_id}/traces endpoint."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

INCIDENT_ID = "abc123"
NOW = datetime(2026, 2, 25, 12, 0, 0, tzinfo=timezone.utc)

# A minimal run_row — only needs to be truthy for the existence check
FAKE_RUN_ROW = {"incident_id": INCIDENT_ID, "run_id": "run_001"}

FAKE_TRACES = [
    {
        "trace_id": 1,
        "incident_id": INCIDENT_ID,
        "step_index": 0,
        "step_type": "retrieve_evidence",
        "input_json": {"run_id": "run_001"},
        "output_json": {"failed_node_count": 2},
        "model_name": None,
        "token_usage": None,
        "latency_ms": 15,
        "created_at": NOW,
    },
    {
        "trace_id": 2,
        "incident_id": INCIDENT_ID,
        "step_index": 1,
        "step_type": "triage",
        "input_json": {"failed_node_count": 2},
        "output_json": {"hypothesis_count": 1},
        "model_name": None,
        "token_usage": None,
        "latency_ms": 3,
        "created_at": NOW,
    },
    {
        "trace_id": 3,
        "incident_id": INCIDENT_ID,
        "step_index": 2,
        "step_type": "llm_call",
        "input_json": {"messages_count": 1, "iteration": 0},
        "output_json": {"stop_reason": "end_turn", "content_types": ["text"]},
        "model_name": "claude-sonnet-4-20250514",
        "token_usage": {"input_tokens": 500, "output_tokens": 200},
        "latency_ms": 2100,
        "created_at": NOW,
    },
]


# ---- Happy path ----


@patch("app.main.get_agent_traces", return_value=FAKE_TRACES)
@patch("app.main.get_run_by_incident", return_value=FAKE_RUN_ROW)
def test_returns_traces(mock_run, mock_traces):
    resp = client.get(f"/incidents/{INCIDENT_ID}/traces")
    assert resp.status_code == 200

    data = resp.json()
    assert len(data) == 3

    mock_run.assert_called_once_with(INCIDENT_ID)
    mock_traces.assert_called_once_with(INCIDENT_ID)


@patch("app.main.get_agent_traces", return_value=FAKE_TRACES)
@patch("app.main.get_run_by_incident", return_value=FAKE_RUN_ROW)
def test_trace_fields_present(mock_run, mock_traces):
    resp = client.get(f"/incidents/{INCIDENT_ID}/traces")
    data = resp.json()

    required_fields = {
        "trace_id", "incident_id", "step_index", "step_type",
        "input_json", "output_json", "model_name", "token_usage",
        "latency_ms", "created_at",
    }
    for trace in data:
        assert required_fields.issubset(trace.keys())


@patch("app.main.get_agent_traces", return_value=FAKE_TRACES)
@patch("app.main.get_run_by_incident", return_value=FAKE_RUN_ROW)
def test_traces_ordered_by_step_index(mock_run, mock_traces):
    resp = client.get(f"/incidents/{INCIDENT_ID}/traces")
    data = resp.json()
    indices = [t["step_index"] for t in data]
    assert indices == sorted(indices)


@patch("app.main.get_agent_traces", return_value=FAKE_TRACES)
@patch("app.main.get_run_by_incident", return_value=FAKE_RUN_ROW)
def test_nullable_fields_for_heuristic_trace(mock_run, mock_traces):
    """Heuristic-path traces have model_name and token_usage as None."""
    resp = client.get(f"/incidents/{INCIDENT_ID}/traces")
    data = resp.json()

    heuristic_trace = data[0]  # retrieve_evidence
    assert heuristic_trace["model_name"] is None
    assert heuristic_trace["token_usage"] is None
    assert heuristic_trace["latency_ms"] == 15


@patch("app.main.get_agent_traces", return_value=FAKE_TRACES)
@patch("app.main.get_run_by_incident", return_value=FAKE_RUN_ROW)
def test_llm_trace_has_model_and_tokens(mock_run, mock_traces):
    """LLM call traces include model_name and token_usage."""
    resp = client.get(f"/incidents/{INCIDENT_ID}/traces")
    data = resp.json()

    llm_trace = data[2]  # llm_call
    assert llm_trace["step_type"] == "llm_call"
    assert llm_trace["model_name"] == "claude-sonnet-4-20250514"
    assert llm_trace["token_usage"]["input_tokens"] == 500
    assert llm_trace["token_usage"]["output_tokens"] == 200


# ---- 404 cases ----


@patch("app.main.get_run_by_incident", return_value=None)
def test_returns_404_for_unknown_incident(mock_run):
    resp = client.get("/incidents/nonexistent/traces")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Incident not found"


@patch("app.main.get_run_by_incident", return_value=None)
def test_does_not_query_traces_for_missing_incident(mock_run):
    """get_agent_traces should not be called if the incident doesn't exist."""
    with patch("app.main.get_agent_traces") as mock_traces:
        client.get("/incidents/nonexistent/traces")
        mock_traces.assert_not_called()


# ---- Empty traces ----


@patch("app.main.get_agent_traces", return_value=[])
@patch("app.main.get_run_by_incident", return_value=FAKE_RUN_ROW)
def test_returns_empty_list_when_no_traces(mock_run, mock_traces):
    """Incident exists but has no traces yet (e.g. before agent run)."""
    resp = client.get(f"/incidents/{INCIDENT_ID}/traces")
    assert resp.status_code == 200
    assert resp.json() == []


# ---- Single trace ----


@patch("app.main.get_agent_traces", return_value=[FAKE_TRACES[0]])
@patch("app.main.get_run_by_incident", return_value=FAKE_RUN_ROW)
def test_returns_single_trace(mock_run, mock_traces):
    resp = client.get(f"/incidents/{INCIDENT_ID}/traces")
    data = resp.json()
    assert len(data) == 1
    assert data[0]["trace_id"] == 1
    assert data[0]["step_type"] == "retrieve_evidence"
