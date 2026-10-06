#!/usr/bin/env bash
# Read-only snapshot of what render.py reconciles against. Usage: live.sh PROJECT REGION [DECL_JSON] > live.json
# With DECL_JSON, `secrets` lists which of its Secret Manager secrets exist (describe only, never values).
set -euo pipefail
P="$1" R="$2"
secrets='[]'
if [[ -n "${3:-}" ]]; then
  # malformed `secrets:` -> empty here; render.py reports it
  secrets=$(jq -r --arg env "${ENVIRONMENT:-}" '.secrets | objects | .[] | tostring | gsub("\\{env\\}"; $env)
      | split(":")[0]' "$3" | sort -u | while read -r s; do
    if gcloud secrets describe "$s" --project="$P" --format='value(name)' </dev/null >/dev/null; then echo "$s"; fi
  done | jq -Rn '[inputs]')
fi
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
  --argjson secrets "$secrets" \
  '{$jobs, $workflows, $schedulers, $metrics, $policies, $secrets}'
