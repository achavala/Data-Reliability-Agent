from __future__ import annotations

from typing import Any

import httpx

from app.config import settings
from app.db import add_approval, insert_audit_event


def _build_incident_blocks(
    incident_id: str,
    triage: dict[str, Any],
    remediation: dict[str, Any],
    validation: dict[str, Any],
    pr_url: str | None = None,
) -> list[dict[str, Any]]:
    """Build Slack Block Kit message for the 'Ring camera moment' incident notification."""
    blast = triage.get("blast_radius", {})
    hypotheses = triage.get("root_cause_hypotheses", [])
    top_cause = hypotheses[0]["cause"] if hypotheses else "unknown"
    confidence = hypotheses[0].get("confidence", 0) if hypotheses else 0
    impacted_count = blast.get("impacted_model_count", "?")
    impacted_nodes = blast.get("impacted_nodes", [])

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f":rotating_light: Data Incident: {incident_id[:16]}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Likely root cause*: {top_cause} (confidence: {confidence:.0%})\n"
                    f"*Blast radius*: {impacted_count} models, "
                    f"{len(impacted_nodes)} downstream nodes\n"
                    f"*Proposed fix*: {remediation.get('strategy', 'N/A')}\n"
                    f"*Risk*: {remediation.get('risk', 'N/A')}"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Validation*: compile={validation.get('dbt_compile', '?')} | "
                    f"test={validation.get('dbt_test', '?')} | "
                    f"safety={validation.get('safety_checks', '?')}"
                ),
            },
        },
    ]

    if pr_url:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*PR*: <{pr_url}|View Pull Request>"},
            }
        )

    blocks.append(
        {
            "type": "actions",
            "block_id": f"approval_{incident_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve_incident",
                    "value": incident_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "reject_incident",
                    "value": incident_id,
                },
            ],
        }
    )

    return blocks


async def post_incident_notification(
    incident_id: str,
    triage: dict[str, Any],
    remediation: dict[str, Any],
    validation: dict[str, Any],
    pr_url: str | None = None,
) -> dict[str, Any] | None:
    """Post incident summary to Slack channel with approval buttons."""
    if not settings.slack_bot_token:
        return None

    blocks = _build_incident_blocks(incident_id, triage, remediation, validation, pr_url)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            json={
                "channel": settings.slack_channel_id,
                "text": f"Data Incident: {incident_id}",
                "blocks": blocks,
            },
        )
        data = resp.json()

    if data.get("ok"):
        insert_audit_event(
            incident_id,
            "slack_notification_sent",
            {"channel": settings.slack_channel_id, "message_ts": data.get("ts")},
        )
        return {"message_ts": data.get("ts"), "channel": settings.slack_channel_id}

    insert_audit_event(
        incident_id,
        "slack_notification_failed",
        {"error": data.get("error", "unknown")},
    )
    return None


async def handle_slack_interaction(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle Slack interactive message callback (approve/reject button clicks)."""
    actions = payload.get("actions", [])
    user = payload.get("user", {}).get("name", "unknown")

    for action in actions:
        action_id = action.get("action_id")
        incident_id = action.get("value")

        if not incident_id:
            continue

        if action_id == "approve_incident":
            add_approval(incident_id, user, "approve", "Approved via Slack")
            insert_audit_event(incident_id, "slack_approval", {"approver": user, "decision": "approve"})
            return {"text": f"Incident {incident_id} approved by {user}"}

        elif action_id == "reject_incident":
            add_approval(incident_id, user, "reject", "Rejected via Slack")
            insert_audit_event(incident_id, "slack_approval", {"approver": user, "decision": "reject"})
            return {"text": f"Incident {incident_id} rejected by {user}"}

    return {"text": "Unknown action"}
