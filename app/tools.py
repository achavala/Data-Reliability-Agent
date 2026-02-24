from __future__ import annotations

import json
from typing import Any

from app.lineage import LineageGraph

TOOL_SCHEMAS = [
    {
        "name": "retrieve_evidence",
        "description": (
            "Retrieve evidence from the dbt run results and manifest for a failed pipeline run. "
            "Returns failed nodes with error messages, compiled SQL, upstream/downstream lineage, "
            "and schema drift signals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The pipeline run ID to retrieve evidence for"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "query_lineage",
        "description": (
            "Query the dbt lineage graph for a specific model. Returns upstream dependencies, "
            "downstream consumers, and blast radius analysis including impacted exposures and metrics."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "The dbt unique_id (e.g. model.analytics.orders)"},
                "direction": {
                    "type": "string",
                    "enum": ["upstream", "downstream", "blast_radius"],
                    "description": "Direction to traverse or 'blast_radius' for full downstream impact analysis",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum traversal depth",
                    "default": 5,
                },
            },
            "required": ["model_id"],
        },
    },
    {
        "name": "search_similar_incidents",
        "description": (
            "Search the vector store for similar past incidents. Returns past triage results "
            "and remediation strategies that were used for similar failures."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Description of the current incident or error message"},
                "limit": {"type": "integer", "description": "Number of similar incidents to return", "default": 3},
            },
            "required": ["query"],
        },
    },
    {
        "name": "propose_patch",
        "description": (
            "Submit a proposed SQL patch to fix the identified issue. Provide the remediation strategy, "
            "a description of the fix, and the actual SQL code for the patched model."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model_id": {"type": "string", "description": "The dbt model unique_id to patch"},
                "strategy": {
                    "type": "string",
                    "description": "The remediation strategy (e.g. 'upstream_schema_drift', 'add_null_check')",
                },
                "description": {"type": "string", "description": "Human-readable description of what the patch does"},
                "patch_sql": {"type": "string", "description": "The complete SQL for the patched model"},
                "original_sql": {
                    "type": "string",
                    "description": "The original compiled SQL of the model (for reference)",
                },
            },
            "required": ["model_id", "strategy", "description", "patch_sql"],
        },
    },
]


def execute_tool(tool_name: str, tool_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call to its implementation.

    context keys:
        run_row: dict — the pipeline run DB row
        vector_store: VectorStore instance
        incident_id: str
        lineage_graph: LineageGraph instance (built lazily)
    """
    if tool_name == "retrieve_evidence":
        return _tool_retrieve_evidence(context)
    elif tool_name == "query_lineage":
        return _tool_query_lineage(tool_input, context)
    elif tool_name == "search_similar_incidents":
        return _tool_search_similar(tool_input, context)
    elif tool_name == "propose_patch":
        return _tool_propose_patch(tool_input, context)
    else:
        return {"error": f"Unknown tool: {tool_name}"}


def _tool_retrieve_evidence(context: dict[str, Any]) -> dict[str, Any]:
    from app.agent import retrieve_evidence

    run_row = context["run_row"]
    return retrieve_evidence(run_row)


def _tool_query_lineage(tool_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    graph: LineageGraph | None = context.get("lineage_graph")
    if graph is None:
        run_row = context["run_row"]
        manifest = run_row.get("manifest_json", {})
        graph = LineageGraph(manifest)
        context["lineage_graph"] = graph

    model_id = tool_input["model_id"]
    direction = tool_input.get("direction", "blast_radius")
    max_depth = tool_input.get("max_depth", 5)

    if direction == "upstream":
        return {"model_id": model_id, "direction": "upstream", "nodes": graph.get_upstream(model_id, max_depth)}
    elif direction == "downstream":
        return {"model_id": model_id, "direction": "downstream", "nodes": graph.get_downstream(model_id, max_depth)}
    else:
        return {"model_id": model_id, **graph.blast_radius(model_id, max_depth)}


def _tool_search_similar(tool_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    vector_store = context.get("vector_store")
    if vector_store is None:
        return {"results": [], "note": "Vector store not available"}

    query = tool_input["query"]
    limit = tool_input.get("limit", 3)
    results = vector_store.search_similar_incidents(query, limit=limit)
    return {"results": results}


def _tool_propose_patch(tool_input: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_id": tool_input["model_id"],
        "strategy": tool_input["strategy"],
        "description": tool_input["description"],
        "patch_sql": tool_input["patch_sql"],
        "original_sql": tool_input.get("original_sql", ""),
        "status": "proposed",
    }
