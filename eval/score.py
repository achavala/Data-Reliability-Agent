from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Heuristic predictor (original, used in quick mode)
# ---------------------------------------------------------------------------

def predict_cause(message: str) -> str:
    lower = message.lower()
    if "does not exist" in lower:
        return "upstream_schema_drift"
    if "type" in lower and ("varchar" in lower or "numeric" in lower or "expected" in lower):
        return "upstream_schema_drift"
    if "ambiguous" in lower and "column" in lower:
        return "upstream_schema_drift"
    if "renamed" in lower or "column rename" in lower:
        return "upstream_schema_drift"
    if "schema change" in lower or "schema drift" in lower or "upstream migration" in lower:
        return "upstream_schema_drift"
    if "source" in lower and ("mixed root" in lower or "multi-source" in lower):
        return "upstream_schema_drift"
    if "freshness" in lower or "last loaded" in lower or "last update" in lower or "not loaded" in lower:
        return "source_freshness_failure"
    return "transformation_logic_error"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def precision_recall_f1(predictions: list[str], actuals: list[str]) -> dict[str, Any]:
    """Calculate per-class and macro precision, recall, F1."""
    classes = sorted(set(actuals) | set(predictions))
    per_class: dict[str, dict[str, float]] = {}

    for cls in classes:
        tp = sum(1 for p, a in zip(predictions, actuals) if p == cls and a == cls)
        fp = sum(1 for p, a in zip(predictions, actuals) if p == cls and a != cls)
        fn = sum(1 for p, a in zip(predictions, actuals) if p != cls and a == cls)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        per_class[cls] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": tp + fn,
        }

    macro_p = sum(c["precision"] for c in per_class.values()) / len(per_class) if per_class else 0
    macro_r = sum(c["recall"] for c in per_class.values()) / len(per_class) if per_class else 0
    macro_f1 = sum(c["f1"] for c in per_class.values()) / len(per_class) if per_class else 0

    accuracy = sum(1 for p, a in zip(predictions, actuals) if p == a) / len(actuals) if actuals else 0

    return {
        "accuracy": round(accuracy, 4),
        "macro_precision": round(macro_p, 4),
        "macro_recall": round(macro_r, 4),
        "macro_f1": round(macro_f1, 4),
        "per_class": per_class,
    }


def patch_quality_score(patch: str, expected_contains: list[str], expected_cause: str) -> float:
    """Score patch quality 0-1.
    - Addresses root cause? (0.4)
    - Contains expected elements? (0.3)
    - Syntactically valid SQL? (0.2)
    - Passes safety checks? (0.1)
    """
    if not patch:
        return 0.0

    score = 0.0
    lowered = patch.lower()

    # Root cause addressed (0.4): patch mentions the cause-related keywords
    cause_keywords = {
        "upstream_schema_drift": ["coalesce", "cast", "rename", "alias", "ifnull", "nullif", "contract"],
        "transformation_logic_error": ["where", "distinct", "qualify", "row_number", "group by", "coalesce", "greatest"],
        "source_freshness_failure": [],
    }
    keywords = cause_keywords.get(expected_cause, [])
    if keywords:
        matched = sum(1 for k in keywords if k in lowered)
        score += 0.4 * min(matched / max(len(keywords), 1), 1.0)
    else:
        score += 0.2  # Partial credit for freshness (no patch expected)

    # Contains expected elements (0.3)
    if expected_contains:
        matched = sum(1 for e in expected_contains if e.lower() in lowered)
        score += 0.3 * (matched / len(expected_contains))
    else:
        score += 0.3  # Full credit when no specific elements expected

    # Syntactically valid SQL (0.2): basic heuristic — has SELECT or comment
    if re.search(r"\b(select|with|insert|update|create)\b", lowered) or patch.strip().startswith("--"):
        score += 0.2

    # Safety checks (0.1): no destructive patterns
    destructive = [r"\bdrop\s+table\b", r"\btruncate\b", r"\bdelete\s+from\b"]
    if not any(re.search(p, lowered) for p in destructive):
        score += 0.1

    return round(score, 4)


def blast_radius_accuracy(predicted_count: int, expected_min: int) -> float:
    """Score blast radius prediction. 1.0 if predicted >= expected_min, scaled below."""
    if expected_min == 0:
        return 1.0
    if predicted_count >= expected_min:
        return 1.0
    return round(predicted_count / expected_min, 4)


# ---------------------------------------------------------------------------
# Eval runners
# ---------------------------------------------------------------------------

def _run_quick_eval(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Heuristic-only eval (original behavior)."""
    predictions = []
    actuals = []

    for row in rows:
        pred = predict_cause(row["message"])
        predictions.append(pred)
        actuals.append(row["expected_cause"])

    metrics = precision_recall_f1(predictions, actuals)
    metrics["mode"] = "quick"
    metrics["total"] = len(rows)
    metrics["correct"] = sum(1 for p, a in zip(predictions, actuals) if p == a)

    # Breakdown by difficulty
    by_difficulty: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "correct": 0})
    for row, pred in zip(rows, predictions):
        d = row.get("difficulty", "unknown")
        by_difficulty[d]["total"] += 1
        if pred == row["expected_cause"]:
            by_difficulty[d]["correct"] += 1

    metrics["by_difficulty"] = {
        k: {**v, "accuracy": round(v["correct"] / v["total"], 4) if v["total"] else 0}
        for k, v in by_difficulty.items()
    }

    return metrics


def _run_full_eval(rows: list[dict[str, Any]], base_url: str) -> dict[str, Any]:
    """Full agent eval: ingest -> agent run -> score all outputs."""
    import httpx

    predictions = []
    actuals = []
    patch_scores = []
    blast_scores = []
    errors = []

    for row in rows:
        try:
            # 1. Ingest the run
            ingest_payload = {
                "pipeline_name": f"eval-{row['incident_id']}",
                "environment": "eval",
                "run_id": row["incident_id"],
                "status": "failed",
                "manifest": row.get("manifest", {}),
                "run_results": row.get("run_results", {}),
            }
            resp = httpx.post(f"{base_url}/ingest/dbt_run", json=ingest_payload, timeout=30)
            resp.raise_for_status()
            incident_id = resp.json()["incident_id"]

            # 2. Run agent
            agent_resp = httpx.post(
                f"{base_url}/agent/run",
                json={"incident_id": incident_id, "approval_required": False},
                timeout=120,
            )
            agent_resp.raise_for_status()
            result = agent_resp.json()

            # 3. Score root cause
            hypotheses = result.get("triage", {}).get("root_cause_hypotheses", [])
            predicted_cause = hypotheses[0]["cause"] if hypotheses else "unknown"
            predictions.append(predicted_cause)
            actuals.append(row["expected_cause"])

            # 4. Score patch quality
            if "expected_patch_contains" in row:
                ps = patch_quality_score(
                    result.get("proposed_patch", ""),
                    row["expected_patch_contains"],
                    row["expected_cause"],
                )
                patch_scores.append(ps)

            # 5. Score blast radius
            if "expected_blast_radius_min" in row:
                br = result.get("triage", {}).get("blast_radius", {}).get("impacted_model_count", 0)
                blast_scores.append(blast_radius_accuracy(br, row["expected_blast_radius_min"]))

        except Exception as e:
            errors.append({"incident_id": row["incident_id"], "error": str(e)})
            predictions.append("error")
            actuals.append(row["expected_cause"])

    metrics = precision_recall_f1(predictions, actuals)
    metrics["mode"] = "full"
    metrics["total"] = len(rows)
    metrics["errors"] = errors
    metrics["avg_patch_quality"] = round(sum(patch_scores) / len(patch_scores), 4) if patch_scores else 0
    metrics["avg_blast_accuracy"] = round(sum(blast_scores) / len(blast_scores), 4) if blast_scores else 0

    return metrics


def _run_regression_eval(rows: list[dict[str, Any]], trace_dir: str | None) -> dict[str, Any]:
    """Compare current agent behavior against stored baseline traces."""
    traces_path = Path(trace_dir) if trace_dir else Path(__file__).parent / "traces"

    if not traces_path.exists():
        return {"mode": "regression", "error": f"Trace directory not found: {traces_path}"}

    baseline_files = list(traces_path.glob("*.json"))
    if not baseline_files:
        return {"mode": "regression", "error": "No baseline traces found", "trace_dir": str(traces_path)}

    baselines: dict[str, dict[str, Any]] = {}
    for f in baseline_files:
        data = json.loads(f.read_text())
        baselines[data.get("incident_id", f.stem)] = data

    regressions = []
    improvements = []
    unchanged = []

    for row in rows:
        iid = row["incident_id"]
        if iid not in baselines:
            continue

        baseline = baselines[iid]
        current_pred = predict_cause(row["message"])
        baseline_pred = baseline.get("predicted_cause", "")

        if current_pred == row["expected_cause"] and baseline_pred != row["expected_cause"]:
            improvements.append(iid)
        elif current_pred != row["expected_cause"] and baseline_pred == row["expected_cause"]:
            regressions.append(iid)
        else:
            unchanged.append(iid)

    return {
        "mode": "regression",
        "total_compared": len(baselines),
        "regressions": regressions,
        "improvements": improvements,
        "unchanged": len(unchanged),
        "regression_count": len(regressions),
        "improvement_count": len(improvements),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="DRA Eval Harness")
    parser.add_argument("--mode", choices=["quick", "full", "regression"], default="quick")
    parser.add_argument("--incidents", type=str, default=None, help="Path to incidents JSONL file")
    parser.add_argument("--traces", type=str, default=None, help="Path to baseline traces directory")
    parser.add_argument("--base-url", type=str, default="http://localhost:8000", help="API base URL for full mode")
    parser.add_argument("--output", type=str, default=None, help="Write results to JSON file")
    args = parser.parse_args()

    path = Path(args.incidents) if args.incidents else Path(__file__).with_name("incidents.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    if args.mode == "quick":
        results = _run_quick_eval(rows)
    elif args.mode == "full":
        results = _run_full_eval(rows, args.base_url)
    elif args.mode == "regression":
        results = _run_regression_eval(rows, args.traces)
    else:
        results = {"error": f"Unknown mode: {args.mode}"}

    print(json.dumps(results, indent=2))

    if args.output:
        Path(args.output).write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
