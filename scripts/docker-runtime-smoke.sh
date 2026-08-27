#!/usr/bin/env bash
set -euo pipefail

project="parseflow-smoke-$RANDOM"
port="${PARSEFLOW_SMOKE_PORT:-18080}"
export PARSEFLOW_DATA_VOLUME="${project}-data"
export PARSEFLOW_MODELS_VOLUME="${project}-models"
cleanup() { docker compose -p "$project" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

PARSEFLOW_PORT="$port" docker compose -p "$project" up --build -d
ready=false
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:$port/ready" >/dev/null 2>&1; then ready=true; break; fi
  sleep 2
done
if [ "$ready" != true ]; then
  docker compose -p "$project" ps >&2
  docker compose -p "$project" logs --tail=200 parseflow >&2
  exit 1
fi
health="$(curl -fsS "http://127.0.0.1:$port/health")"
readiness="$(curl -fsS "http://127.0.0.1:$port/ready")"
metrics="$(curl -fsS "http://127.0.0.1:$port/metrics")"
grep -q '"status":"ok"' <<< "$health"
grep -q '"status":"ready"' <<< "$readiness"
grep -q 'parseflow_task_queue_depth' <<< "$metrics"

container="$(docker compose -p "$project" ps -q parseflow)"
test -n "$container"
test "$(docker inspect -f '{{.Config.User}}' "$container")" = "parseflow"
test "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$container")" = "true"
test "$(docker inspect -f '{{json .HostConfig.CapDrop}}' "$container")" = '["ALL"]'

response="$(curl -fsS -F 'file=@-;filename=smoke.txt;type=text/plain' "http://127.0.0.1:$port/api/v1/tasks/parse" <<< 'ParseFlow runtime smoke')"
task_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])' <<< "$response")"
for _ in $(seq 1 60); do
  task="$(curl -fsS "http://127.0.0.1:$port/api/v1/tasks/$task_id")"
  status="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<< "$task")"
  case "$status" in succeeded|partial) break;; failed|cancelled|interrupted) echo "$task"; exit 1;; esac
  sleep 1
done
test "$status" = "succeeded" -o "$status" = "partial"
export_url="$(python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; assert r["quality"]["input_classification"] == "txt"; assert r["provenance"]["provider_chain"]; print(next(a["download_url"] for a in r["artifacts"] if a["filename"] == "document.txt"))' <<< "$task")"
test "$(curl -fsS "http://127.0.0.1:$port$export_url")" = "ParseFlow runtime smoke"
events="$(curl -fsS "http://127.0.0.1:$port/api/v1/tasks/$task_id/events")"
python3 -c 'import json,sys; x=json.load(sys.stdin); assert x["items"][-1]["type"] in {"succeeded","partial"}' <<< "$events"
if docker compose -p "$project" logs 2>&1 | grep -E '/data/|/app/data|API_KEY='; then
  echo 'runtime logs leaked a protected path or credential marker' >&2
  exit 1
fi

echo "ParseFlow Docker runtime smoke passed"
