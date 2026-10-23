#!/usr/bin/env bash
# End-to-end check against a deployed stack: auth -> create -> poll -> read -> delete.
#
#   API_URL=$(terraform -chdir=infra/app output -raw api_url) \
#   TOKEN=$(python scripts/netloom_auth.py login --email me@example.com --password '...') \
#   ./scripts/smoke_test.sh
#
# Requires: curl, jq.
set -euo pipefail

: "${API_URL:?set API_URL}"
: "${TOKEN:?set TOKEN}"
API_URL="${API_URL%/}"
AUTH=(-H "Authorization: Bearer ${TOKEN}")
JSON=(-H "Content-Type: application/json")
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

wait_for() { # $1 = id, $2 = wanted status
  for _ in $(seq 1 60); do
    status=$(curl -fsS "${AUTH[@]}" "${API_URL}/v1/networks/$1" | jq -r .status)
    printf '   status=%s\n' "$status"
    [[ "$status" == "$2" ]] && return 0
    [[ "$status" == "FAILED" ]] && { curl -fsS "${AUTH[@]}" "${API_URL}/v1/networks/$1/events" | jq .; return 1; }
    sleep 5
  done
  echo "timed out waiting for $2" >&2; return 1
}

step "health (public)"
curl -fsS "${API_URL}/v1/health" | jq .

step "API documentation (public)"
curl -fsS "${API_URL}/v1/openapi.json" | jq -r '"   \(.info.title) \(.info.version) - \(.paths | length) paths"'
curl -fsS -o /dev/null -w '   Swagger UI %{http_code}: '"${API_URL}"'/docs\n' "${API_URL}/docs"

step "request without a token is rejected"
code=$(curl -s -o /dev/null -w '%{http_code}' "${API_URL}/v1/networks")
[[ "$code" == "401" ]] && echo "   401 as expected" || { echo "expected 401, got $code"; exit 1; }

step "who am I"
curl -fsS "${AUTH[@]}" "${API_URL}/v1/me" | jq .

step "dry-run plan"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "${API_URL}/v1/plans" \
  -d '{"name":"smoke","cidr":"10.77.0.0/16","layout":{"tiers":["public","private"],"zones":2,"prefix":24}}' | jq .plan.summary

step "create network"
id=$(curl -fsS "${AUTH[@]}" "${JSON[@]}" -H "Idempotency-Key: smoke-$(date +%s)" -X POST "${API_URL}/v1/networks" \
  -d '{"name":"smoke","cidr":"10.77.0.0/16","layout":{"tiers":["public","private"],"zones":2,"prefix":24},"tags":{"purpose":"smoke-test"}}' \
  | jq -r .id)
echo "   id=$id"
wait_for "$id" AVAILABLE

step "network + subnets"
curl -fsS "${AUTH[@]}" "${API_URL}/v1/networks/$id" | jq '{id,status,vpc_id,internet_gateway_id}'
curl -fsS "${AUTH[@]}" "${API_URL}/v1/networks/$id/subnets" | jq -r '.items[] | [.name,.cidr,.availability_zone,.subnet_id] | @tsv'

step "delete network"
curl -fsS "${AUTH[@]}" -X DELETE "${API_URL}/v1/networks/$id" | jq -r .status
wait_for "$id" DELETED

step "audit trail"
curl -fsS "${AUTH[@]}" "${API_URL}/v1/networks/$id/events" | jq -r '.items[] | [.at,.type,.actor.username,.message] | @tsv'
echo -e "\nSmoke test passed."
