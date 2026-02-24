#!/usr/bin/env bash
set -euo pipefail

BASE_URL=${BASE_URL:-http://localhost:8000}

INGEST_RESPONSE=$(curl -s -X POST "$BASE_URL/ingest/dbt_run" \
  -H "Content-Type: application/json" \
  --data @scripts/sample_ingest.json)

echo "Ingest: $INGEST_RESPONSE"
INCIDENT_ID=$(echo "$INGEST_RESPONSE" | sed -n 's/.*"incident_id":"\([^"]*\)".*/\1/p')

AGENT_PAYLOAD=$(printf '{"incident_id":"%s","approval_required":true}' "$INCIDENT_ID")
AGENT_RESPONSE=$(curl -s -X POST "$BASE_URL/agent/run" -H "Content-Type: application/json" -d "$AGENT_PAYLOAD")
echo "Agent: $AGENT_RESPONSE"

APPROVAL_PAYLOAD=$(printf '{"incident_id":"%s","approver":"oncall@datacorp.com","decision":"approve","comment":"safe to merge"}' "$INCIDENT_ID")
APPROVAL_RESPONSE=$(curl -s -X POST "$BASE_URL/approvals" -H "Content-Type: application/json" -d "$APPROVAL_PAYLOAD")
echo "Approval: $APPROVAL_RESPONSE"
