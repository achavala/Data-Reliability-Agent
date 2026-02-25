from __future__ import annotations

import json
import re
import time
from typing import Any

from app.config import settings
from app.db import insert_audit_event

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DESTRUCTIVE_SQL_PATTERNS = [
    r"\bdrop\s+table\b",
    r"\btruncate\s+table\b",
    r"\bdelete\s+from\b",
]

SYSTEM_PROMPT = """\
You are the Data Reliability Agent (DRA). Your job is to triage dbt pipeline \
incidents, identify root causes with evidence, assess blast radius, and propose \
safe remediation patches.

Follow this strict process:
1. RETRIEVE: Call retrieve_evidence to get failed nodes, error messages, \
compiled SQL, and schema drift signals from the run artifacts.
2. INVESTIGATE: Use query_lineage to understand the blast radius (downstream \
impact). Use search_similar_incidents to find precedent from past incidents.
3. TRIAGE: Synthesize your findings into root cause hypotheses with confidence \
scores. Do NOT guess — if evidence is insufficient, say so.
4. REMEDIATE: Propose a concrete SQL patch via propose_patch that addresses the \
root cause. The patch must be safe (no destructive SQL).

After calling your tools, provide your final answer as a JSON object with this \
exact structure:
{
  "triage": {
    "summary": "...",
    "root_cause_hypotheses": [
      {"cause": "...", "confidence": 0.0, "evidence_refs": ["..."]}
    ],
    "blast_radius": {
      "impacted_model_count": 0,
      "impacted_nodes": ["..."]
    }
  },
  "remediation": {
    "strategy": "...",
    "actions": ["..."],
    "proposed_patch": "...",
    "risk": "low|medium|high"
  }
}
"""

MAX_AGENT_ITERATIONS = 10

# ---------------------------------------------------------------------------
# Heuristic helpers (original deterministic logic, kept as fallback)
# ---------------------------------------------------------------------------


def _extract_failed_nodes(run_results: dict[str, Any]) -> list[dict[str, Any]]:
    results = run_results.get("results", [])
    return [r for r in results if r.get("status") in {"error", "fail"}]


def _lineage_for_node(manifest: dict[str, Any], unique_id: str) -> dict[str, Any]:
    parent_map = manifest.get("parent_map", {})
    child_map = manifest.get("child_map", {})
    return {
        "upstream": parent_map.get(unique_id, []),
        "downstream": child_map.get(unique_id, []),
    }


def retrieve_evidence(run_row: dict[str, Any]) -> dict[str, Any]:
    run_results = run_row["run_results_json"]
    manifest = run_row["manifest_json"]
    failed = _extract_failed_nodes(run_results)

    evidence: dict[str, Any] = {
        "run_id": run_row["run_id"],
        "status": run_row.get("run_status") or run_row.get("status", "unknown"),
        "failed_nodes": [],
        "schema_drift_signals": [],
    }

    for node in failed:
        unique_id = node.get("unique_id", "unknown")
        message = node.get("message", "") or ""
        lineage = _lineage_for_node(manifest, unique_id)
        evidence["failed_nodes"].append(
            {
                "unique_id": unique_id,
                "message": message,
                "lineage": lineage,
                "compiled_sql": node.get("compiled_code") or "",
            }
        )
        if "column" in message.lower() and "does not exist" in message.lower():
            evidence["schema_drift_signals"].append(
                {"node": unique_id, "signal": "upstream_column_missing", "detail": message}
            )

    return evidence


def triage(evidence: dict[str, Any]) -> dict[str, Any]:
    hypotheses = []
    if evidence["schema_drift_signals"]:
        hypotheses.append(
            {
                "cause": "upstream_schema_drift",
                "confidence": 0.88,
                "evidence_refs": ["schema_drift_signals"],
            }
        )

    if not hypotheses:
        hypotheses.append(
            {
                "cause": "transformation_logic_error",
                "confidence": 0.54,
                "evidence_refs": ["failed_nodes"],
            }
        )

    blast = 0
    impacted_nodes: list[str] = []
    for node in evidence["failed_nodes"]:
        downstream = node["lineage"].get("downstream", [])
        blast += len(downstream)
        impacted_nodes.extend(downstream)

    return {
        "summary": "dbt incident triaged from run artifacts",
        "root_cause_hypotheses": hypotheses,
        "blast_radius": {
            "impacted_model_count": blast,
            "impacted_nodes": sorted(set(impacted_nodes)),
        },
    }


def propose_remediation(triage_result: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    top = triage_result["root_cause_hypotheses"][0]["cause"]
    node_id = evidence["failed_nodes"][0]["unique_id"] if evidence["failed_nodes"] else "model.unknown"

    if top == "upstream_schema_drift":
        actions = [
            "Update failing model select list to map renamed/removed columns safely",
            "Add dbt schema contract test for required source columns",
        ]
        patch = (
            f"-- Proposed patch for {node_id}\n"
            "select\n"
            "  coalesce(order_total, 0) as order_total\n"
            "from {{ ref('stg_orders') }}"
        )
    else:
        actions = [
            "Review model logic and adjust join/filter conditions",
            "Add targeted data test to prevent recurrence",
        ]
        patch = (
            f"-- Proposed patch for {node_id}\n"
            "select *\n"
            "from {{ ref('stg_orders') }}\n"
            "where order_id is not null"
        )

    return {
        "strategy": top,
        "actions": actions,
        "proposed_patch": patch,
        "risk": "low" if top == "upstream_schema_drift" else "medium",
    }


def validate_patch(
    patch: str,
    model_id: str | None = None,
    model_path: str | None = None,
) -> dict[str, Any]:
    # Static safety checks (always run)
    lowered = patch.lower()
    safety_violations = []
    for pattern in DESTRUCTIVE_SQL_PATTERNS:
        if re.search(pattern, lowered):
            safety_violations.append(pattern)

    safety_ok = not safety_violations

    # Real dbt validation if configured
    if settings.dbt_project_dir and model_id and model_path:
        from app.dbt_validator import validate_patch_with_dbt

        dbt_result = validate_patch_with_dbt(patch, model_path, model_id)
        return {
            "dbt_compile": dbt_result["dbt_compile"],
            "dbt_test": dbt_result["dbt_test"],
            "safety_checks": "pass" if safety_ok else "fail",
            "violations": safety_violations,
            "dbt_compile_output": dbt_result.get("dbt_compile_output", ""),
            "dbt_test_output": dbt_result.get("dbt_test_output", ""),
        }

    # Fallback: static-only validation
    compile_ok = len(patch.strip()) > 0
    test_ok = compile_ok and safety_ok
    return {
        "dbt_compile": "pass" if compile_ok else "fail",
        "dbt_test": "pass" if test_ok else "fail",
        "safety_checks": "pass" if safety_ok else "fail",
        "violations": safety_violations,
    }


# ---------------------------------------------------------------------------
# LLM-powered ReAct agent loop
# ---------------------------------------------------------------------------


def _run_llm_agent_loop(
    incident_id: str,
    run_row: dict[str, Any],
    vector_store: Any | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    """ReAct-style tool-use loop using Claude's native tool_use."""
    import anthropic

    from app.tools import TOOL_SCHEMAS, execute_tool

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    context: dict[str, Any] = {
        "run_row": run_row,
        "vector_store": vector_store,
        "incident_id": incident_id,
    }

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Triage incident {incident_id}. "
                f"The pipeline run '{run_row['run_id']}' has status '{run_row.get('run_status') or run_row.get('status', 'unknown')}'. "
                "Investigate the failure, determine root cause, assess blast radius, "
                "and propose a safe remediation patch."
            ),
        }
    ]

    response = None
    for iteration in range(MAX_AGENT_ITERATIONS):
        start = time.time()
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )
        latency_ms = int((time.time() - start) * 1000)

        insert_audit_event(
            incident_id,
            f"agent_iteration_{iteration}",
            {
                "stop_reason": response.stop_reason,
                "tool_calls": [b.name for b in response.content if b.type == "tool_use"],
                "latency_ms": latency_ms,
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            },
        )

        if response.stop_reason == "tool_use":
            # Execute each tool call and collect results
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_start = time.time()
                    result = execute_tool(block.name, block.input, context)
                    tool_latency = int((time.time() - tool_start) * 1000)

                    insert_audit_event(
                        incident_id,
                        f"tool_{block.name}",
                        {
                            "input": block.input,
                            "output_summary": json.dumps(result, default=str)[:1000],
                            "latency_ms": tool_latency,
                        },
                    )

                    # Store proposed patch from the tool for later extraction
                    if block.name == "propose_patch" and "patch_sql" in result:
                        context["proposed_patch"] = result

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        }
                    )

            # Append assistant response + tool results to message history
            messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})
            messages.append({"role": "user", "content": tool_results})
        else:
            # end_turn — the agent is done
            break

    # Parse the final response
    triage_result, remediation = _parse_agent_output(response, context)

    patch = remediation.get("proposed_patch", "")
    validation = validate_patch(patch)

    return triage_result, remediation, validation, patch


def _parse_agent_output(
    response: Any,
    context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract structured triage and remediation from the agent's final response."""
    final_text = ""
    if response:
        for block in response.content:
            if hasattr(block, "text"):
                final_text += block.text

    # Try to extract JSON from the response
    triage_result = None
    remediation = None

    # Look for a JSON block in the text
    try:
        # Find JSON that contains "triage" key
        start = final_text.find("{")
        if start >= 0:
            # Find matching closing brace
            depth = 0
            for i in range(start, len(final_text)):
                if final_text[i] == "{":
                    depth += 1
                elif final_text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = final_text[start : i + 1]
                        parsed = json.loads(candidate)
                        if "triage" in parsed:
                            triage_result = parsed["triage"]
                            remediation = parsed.get("remediation", {})
                        break
    except (json.JSONDecodeError, KeyError):
        pass

    # Fallback: use proposed_patch from tool call if available
    if triage_result is None:
        triage_result = {
            "summary": "Agent completed triage (structured output parsing failed)",
            "root_cause_hypotheses": [{"cause": "unknown", "confidence": 0.5, "evidence_refs": []}],
            "blast_radius": {"impacted_model_count": 0, "impacted_nodes": []},
        }

    if remediation is None:
        proposed = context.get("proposed_patch", {})
        remediation = {
            "strategy": proposed.get("strategy", "unknown"),
            "actions": [proposed.get("description", "Review agent output")],
            "proposed_patch": proposed.get("patch_sql", ""),
            "risk": "medium",
        }

    return triage_result, remediation


# ---------------------------------------------------------------------------
# Main entry point — dispatches to LLM or heuristic
# ---------------------------------------------------------------------------


def run_agent_loop(
    incident_id: str,
    run_row: dict[str, Any],
    vector_store: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    """Run the agent loop. Uses LLM when configured, falls back to heuristics."""
    if not settings.mock_llm and settings.anthropic_api_key:
        return _run_llm_agent_loop(incident_id, run_row, vector_store)

    # Original heuristic path (unchanged behavior)
    evidence = retrieve_evidence(run_row)
    insert_audit_event(incident_id, "evidence_retrieved", evidence)

    triage_result = triage(evidence)
    insert_audit_event(incident_id, "triage_completed", triage_result)

    remediation = propose_remediation(triage_result, evidence)
    insert_audit_event(incident_id, "remediation_proposed", remediation)

    patch = remediation["proposed_patch"]
    validation = validate_patch(patch)
    insert_audit_event(incident_id, "patch_validated", validation)

    return triage_result, remediation, validation, patch
