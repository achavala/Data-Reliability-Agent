from __future__ import annotations

from collections import deque
from typing import Any

import networkx as nx


class LineageGraph:
    """Builds a directed graph from a dbt manifest for lineage traversal and blast radius analysis."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.graph = nx.DiGraph()
        self._parse_manifest(manifest)

    def _parse_manifest(self, manifest: dict[str, Any]) -> None:
        nodes = manifest.get("nodes", {})
        sources = manifest.get("sources", {})
        exposures = manifest.get("exposures", {})
        metrics = manifest.get("metrics", {})

        for uid, node in nodes.items():
            self.graph.add_node(uid, **self._node_attrs(node, "model"))
        for uid, src in sources.items():
            self.graph.add_node(uid, **self._node_attrs(src, "source"))
        for uid, exp in exposures.items():
            self.graph.add_node(uid, **self._node_attrs(exp, "exposure"))
        for uid, met in metrics.items():
            self.graph.add_node(uid, **self._node_attrs(met, "metric"))

        # Build edges from depends_on in nodes
        for uid, node in nodes.items():
            for dep in node.get("depends_on", {}).get("nodes", []):
                if dep not in self.graph:
                    self.graph.add_node(dep, resource_type="unknown", name=dep.split(".")[-1])
                self.graph.add_edge(dep, uid, edge_type="depends_on")

        # Build edges from exposures/metrics depends_on
        for uid, exp in exposures.items():
            for dep in exp.get("depends_on", {}).get("nodes", []):
                if dep not in self.graph:
                    self.graph.add_node(dep, resource_type="unknown", name=dep.split(".")[-1])
                self.graph.add_edge(dep, uid, edge_type="exposes")

        for uid, met in metrics.items():
            for dep in met.get("depends_on", {}).get("nodes", []):
                if dep not in self.graph:
                    self.graph.add_node(dep, resource_type="unknown", name=dep.split(".")[-1])
                self.graph.add_edge(dep, uid, edge_type="metric_ref")

        # Legacy parent_map / child_map fallback (used by the sample manifest)
        parent_map = manifest.get("parent_map", {})
        child_map = manifest.get("child_map", {})
        for uid, parents in parent_map.items():
            if uid not in self.graph:
                self.graph.add_node(uid, resource_type="model", name=uid.split(".")[-1])
            for p in parents:
                if p not in self.graph:
                    self.graph.add_node(p, resource_type="model", name=p.split(".")[-1])
                if not self.graph.has_edge(p, uid):
                    self.graph.add_edge(p, uid, edge_type="depends_on")
        for uid, children in child_map.items():
            if uid not in self.graph:
                self.graph.add_node(uid, resource_type="model", name=uid.split(".")[-1])
            for c in children:
                if c not in self.graph:
                    self.graph.add_node(c, resource_type="model", name=c.split(".")[-1])
                if not self.graph.has_edge(uid, c):
                    self.graph.add_edge(uid, c, edge_type="depends_on")

    @staticmethod
    def _node_attrs(node: dict[str, Any], default_type: str) -> dict[str, Any]:
        return {
            "resource_type": node.get("resource_type", default_type),
            "name": node.get("name", node.get("unique_id", "unknown").split(".")[-1]),
            "schema": node.get("schema", ""),
            "description": node.get("description", ""),
            "columns": {
                k: v.get("description", "") for k, v in node.get("columns", {}).items()
            },
        }

    def get_upstream(self, node_id: str, max_depth: int | None = None) -> list[dict[str, Any]]:
        if node_id not in self.graph:
            return []
        return self._bfs(node_id, direction="upstream", max_depth=max_depth)

    def get_downstream(self, node_id: str, max_depth: int | None = None) -> list[dict[str, Any]]:
        if node_id not in self.graph:
            return []
        return self._bfs(node_id, direction="downstream", max_depth=max_depth)

    def _bfs(self, start: str, direction: str, max_depth: int | None) -> list[dict[str, Any]]:
        visited: set[str] = set()
        queue: deque[tuple[str, int]] = deque()
        results: list[dict[str, Any]] = []

        neighbors_fn = self.graph.predecessors if direction == "upstream" else self.graph.successors

        for neighbor in neighbors_fn(start):
            queue.append((neighbor, 1))

        while queue:
            node_id, depth = queue.popleft()
            if node_id in visited:
                continue
            if max_depth is not None and depth > max_depth:
                continue
            visited.add(node_id)
            attrs = self.graph.nodes.get(node_id, {})
            results.append({
                "unique_id": node_id,
                "resource_type": attrs.get("resource_type", "unknown"),
                "name": attrs.get("name", node_id.split(".")[-1]),
                "depth": depth,
            })
            for neighbor in neighbors_fn(node_id):
                if neighbor not in visited:
                    queue.append((neighbor, depth + 1))

        return sorted(results, key=lambda x: (x["depth"], x["unique_id"]))

    def blast_radius(self, node_id: str, max_depth: int = 10) -> dict[str, Any]:
        if node_id not in self.graph:
            return {
                "impacted_model_count": 0,
                "impacted_nodes": [],
                "impacted_exposures": [],
                "impacted_metrics": [],
                "max_depth": 0,
            }

        downstream = self.get_downstream(node_id, max_depth=max_depth)
        exposures = [n["name"] for n in downstream if n["resource_type"] == "exposure"]
        metrics = [n["name"] for n in downstream if n["resource_type"] == "metric"]
        models = [n for n in downstream if n["resource_type"] in ("model", "unknown")]
        actual_max = max((n["depth"] for n in downstream), default=0)

        return {
            "impacted_model_count": len(models),
            "impacted_nodes": downstream,
            "impacted_exposures": exposures,
            "impacted_metrics": metrics,
            "max_depth": actual_max,
        }

    def detect_schema_drift(self, dataset_id: str, schema_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(schema_history) < 2:
            return []

        current = schema_history[0]
        previous = schema_history[1]
        signals: list[dict[str, Any]] = []

        if current.get("schema_version") != previous.get("schema_version"):
            signals.append({
                "dataset_id": dataset_id,
                "signal": "schema_version_changed",
                "from_version": previous.get("schema_version"),
                "to_version": current.get("schema_version"),
                "valid_from": str(current.get("valid_from", "")),
            })

        return signals

    def to_serializable(self, node_id: str | None = None) -> dict[str, Any]:
        if node_id:
            upstream = self.get_upstream(node_id)
            downstream = self.get_downstream(node_id)
            attrs = self.graph.nodes.get(node_id, {})
            return {
                "node_id": node_id,
                "resource_type": attrs.get("resource_type", "unknown"),
                "name": attrs.get("name", ""),
                "upstream": upstream,
                "downstream": downstream,
            }
        return {
            "nodes": [
                {"unique_id": n, **self.graph.nodes[n]}
                for n in self.graph.nodes
            ],
            "edges": [
                {"source": u, "target": v, **d}
                for u, v, d in self.graph.edges(data=True)
            ],
        }
