from __future__ import annotations

import base64
from typing import Any

import httpx

from app.config import settings
from app.db import insert_audit_event

GITHUB_API = "https://api.github.com"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _get_base_sha() -> str:
    """Get the SHA of the base branch HEAD."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{settings.github_repo}/git/ref/heads/{settings.github_base_branch}",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["object"]["sha"]


async def create_branch(branch_name: str, base_sha: str) -> dict[str, Any]:
    """Create a new branch from base_sha."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{settings.github_repo}/git/refs",
            headers=_headers(),
            json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
        )
        resp.raise_for_status()
        return resp.json()


async def commit_patch(
    branch_name: str,
    file_path: str,
    patch_content: str,
    message: str,
) -> dict[str, Any]:
    """Commit a file to a branch using the GitHub Contents API."""
    async with httpx.AsyncClient() as client:
        # Check if the file already exists to get its SHA
        existing_sha = None
        get_resp = await client.get(
            f"{GITHUB_API}/repos/{settings.github_repo}/contents/{file_path}",
            headers=_headers(),
            params={"ref": branch_name},
        )
        if get_resp.status_code == 200:
            existing_sha = get_resp.json()["sha"]

        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(patch_content.encode("utf-8")).decode("utf-8"),
            "branch": branch_name,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        resp = await client.put(
            f"{GITHUB_API}/repos/{settings.github_repo}/contents/{file_path}",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


async def create_pull_request(
    incident_id: str,
    branch_name: str,
    title: str,
    body: str,
) -> dict[str, Any]:
    """Create a PR with structured description."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{GITHUB_API}/repos/{settings.github_repo}/pulls",
            headers=_headers(),
            json={
                "title": title,
                "body": body,
                "head": branch_name,
                "base": settings.github_base_branch,
            },
        )
        resp.raise_for_status()
        pr_data = resp.json()
        return {
            "pr_number": pr_data["number"],
            "pr_url": pr_data["html_url"],
            "status": pr_data["state"],
        }


async def get_pr_status(pr_number: int) -> dict[str, Any]:
    """Get current PR status including checks."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{GITHUB_API}/repos/{settings.github_repo}/pulls/{pr_number}",
            headers=_headers(),
        )
        resp.raise_for_status()
        pr = resp.json()

        # Get check runs for the PR head SHA
        checks_resp = await client.get(
            f"{GITHUB_API}/repos/{settings.github_repo}/commits/{pr['head']['sha']}/check-runs",
            headers=_headers(),
        )
        checks = []
        if checks_resp.status_code == 200:
            checks = [
                {"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion")}
                for c in checks_resp.json().get("check_runs", [])
            ]

        return {
            "pr_number": pr["number"],
            "pr_url": pr["html_url"],
            "status": pr["state"],
            "mergeable": pr.get("mergeable"),
            "checks": checks,
        }


def format_pr_body(
    incident_id: str,
    triage: dict[str, Any],
    remediation: dict[str, Any],
    validation: dict[str, Any],
) -> str:
    """Format a structured PR description with DRA context."""
    hypotheses = triage.get("root_cause_hypotheses", [])
    hyp_lines = ""
    for h in hypotheses:
        hyp_lines += f"- **{h['cause']}** (confidence: {h.get('confidence', '?'):.0%})\n"

    blast = triage.get("blast_radius", {})
    impacted = blast.get("impacted_nodes", [])
    impacted_str = ", ".join(impacted[:10])
    if len(impacted) > 10:
        impacted_str += f" ... and {len(impacted) - 10} more"

    actions = remediation.get("actions", [])
    actions_str = "\n".join(f"- {a}" for a in actions)

    return f"""## DRA Automated Remediation

**Incident ID**: `{incident_id}`

### Root Cause Analysis
{hyp_lines}
### Blast Radius
- **Impacted models**: {blast.get('impacted_model_count', 'unknown')}
- **Impacted nodes**: {impacted_str or 'none identified'}

### Remediation Strategy
- **Strategy**: {remediation.get('strategy', 'unknown')}
- **Risk**: {remediation.get('risk', 'unknown')}

**Actions**:
{actions_str}

### Validation Results
| Check | Result |
|-------|--------|
| dbt compile | {validation.get('dbt_compile', 'N/A')} |
| dbt test | {validation.get('dbt_test', 'N/A')} |
| Safety checks | {validation.get('safety_checks', 'N/A')} |

---
_Generated by Data Reliability Agent_
"""


async def create_pr_for_incident(
    incident_id: str,
    model_path: str,
    patch: str,
    triage: dict[str, Any],
    remediation: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    """Full workflow: create branch -> commit patch -> open PR -> audit."""
    if not settings.github_token or not settings.github_repo:
        return {"error": "GitHub not configured (GITHUB_TOKEN or GITHUB_REPO missing)"}

    branch_name = f"dra/fix-{incident_id[:12]}"

    base_sha = await _get_base_sha()
    await create_branch(branch_name, base_sha)

    await commit_patch(
        branch_name,
        model_path,
        patch,
        f"fix: DRA automated fix for incident {incident_id}",
    )

    body = format_pr_body(incident_id, triage, remediation, validation)
    pr_result = await create_pull_request(
        incident_id,
        branch_name,
        f"fix: automated remediation for {incident_id[:12]}",
        body,
    )

    pr_result["branch_name"] = branch_name
    insert_audit_event(incident_id, "pr_created", pr_result)
    return pr_result
