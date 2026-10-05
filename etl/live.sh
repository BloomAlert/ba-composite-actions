#!/usr/bin/env bash
# Read-only snapshot of what render.py reconciles against. Usage: live.sh PROJECT REGION > live.json
set -euo pipefail
P="$1" R="$2"
jq -n \
  --argjson jobs "$(gcloud run jobs list --project="$P" --region="$R" --format=json \
    | jq '[.[] | {name: .metadata.name, image: .spec.template.spec.template.spec.containers[0].image}]')" \
  --argjson workflows "$(gcloud workflows list --project="$P" --location="$R" --format=json \
    | jq '[.[] | {name: (.name | split("/") | last)}]')" \
  --argjson schedulers "$(gcloud scheduler jobs list --project="$P" --location="$R" --format=json \
    | jq '[.[] | {name: (.name | split("/") | last), state, uri: .httpTarget.uri}]')" \
  --argjson metrics "$(gcloud logging metrics list --project="$P" --filter='name:etl-heartbeat-' --format=json \
    | jq '[.[] | {name}]')" \
  --argjson policies "$(gcloud monitoring policies list --project="$P" --filter='displayName:etl-heartbeat-' --format=json \
    | jq '[.[] | {name, displayName}]')" \
  '{$jobs, $workflows, $schedulers, $metrics, $policies}'
