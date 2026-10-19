#!/usr/bin/env bash
# Guided minisentry demo against the running Docker stack.
#
#   ./demo.sh            run the demo, pausing between steps (press Enter)
#   ./demo.sh --reset    wipe all stored issues first, so counts start at zero
#   ./demo.sh --no-pause run straight through
set -euo pipefail
cd "$(dirname "$0")"

RESET=0; PAUSE=1
for arg in "$@"; do
  case "$arg" in
    --reset) RESET=1 ;;
    --no-pause) PAUSE=0 ;;
    *) echo "unknown option: $arg"; exit 1 ;;
  esac
done

API=http://127.0.0.1:8000
KEY=$(grep '^PROJECT_KEYS=' .env | cut -d= -f2 | cut -d, -f1 | cut -d: -f2)
export MINISENTRY_DSN="http://$KEY@127.0.0.1:8000/1"
PY=.venv/bin/python
APP_PID=""
trap '[[ -n "$APP_PID" ]] && kill "$APP_PID" 2>/dev/null || true' EXIT

bold=$'\e[1m'; cyan=$'\e[36m'; dim=$'\e[2m'; off=$'\e[0m'
step() { echo; echo "${bold}${cyan}== $* ==${off}"; }
run()  { echo "${dim}\$ $*${off}"; }
pause() { if ((PAUSE)); then read -rp "${dim}[Enter to continue]${off} " _; fi; }
api()  { curl -fsS -H "X-Minisentry-Key: $KEY" "$API$1"; }
wait_healthy() {
  for _ in $(seq 60); do curl -fs "$API/healthz" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "API did not come up on $API"; exit 1
}
issues_table() {
  api /api/1/issues | jq -r '.[] | "  \(.times_seen)x\t#\(.id)  \(.title)\t\(.culprit // "")"' | column -t -s $'\t'
}
issue_id() { api /api/1/issues | jq -r --arg t "$1" '[.[] | select(.title | startswith($t))][0].id'; }

[[ -x $PY ]] || { echo "Missing .venv. Run: python3 -m venv .venv && .venv/bin/pip install -e '.[server,dev]'"; exit 1; }

if ((RESET)); then
  step "Resetting: fresh Postgres and Redis"
  run docker compose down -v '&&' docker compose up -d
  docker compose down -v >/dev/null 2>&1
  docker compose up -d >/dev/null 2>&1
  wait_healthy
fi

step "1. The stack: API, worker, Redis, Postgres"
run docker compose ps
docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
run curl $API/healthz
wait_healthy; curl -fsS "$API/healthz"; echo
pause

step "2. An app throws 15 errors through the SDK"
run python examples/trigger_errors.py
$PY examples/trigger_errors.py >/dev/null
echo "  10 KeyErrors (different ids), 3 ValueErrors (different inputs), 2 slow-payment logs"
pause

step "3. ...and minisentry groups them into issues"
run curl $API/api/1/issues
sleep 1; issues_table
echo "  Same bug = same issue, whatever the id, input or line number."
pause

step "4. A live web app with bugs on purpose"
run uvicorn demo_app:app --app-dir examples --port 8001
$PY -m uvicorn demo_app:app --app-dir examples --port 8001 --log-level warning >/dev/null 2>&1 &
APP_PID=$!
for _ in $(seq 30); do (exec 3<>/dev/tcp/127.0.0.1/8001) 2>/dev/null && break; sleep 0.5; done
for req in "/users/1" "/users/2" "/users/42" "/divide?a=1&b=0"; do
  printf '  %-22s -> HTTP %s\n' "GET $req" "$(curl -s -o /dev/null -w '%{http_code}' "localhost:8001$req")"
done
printf '  %-22s -> %s\n' "POST /login" "$(curl -s -X POST localhost:8001/login)"
sleep 2
issues_table
pause

step "5. Inside one event: stack trace, request and breadcrumbs"
ID=$(api /api/1/issues | jq -r '[.[] | select((.culprit // "") | test("get_user"))][0].id')
run curl $API/api/1/issues/$ID/events
api "/api/1/issues/$ID/events" | jq '.[0].payload | {
  exception: .exception.values[-1] | {type, value},
  in_app_frames: [.exception.values[-1].stacktrace.frames[] | select(.in_app) | "\(.function)() line \(.lineno): \(.context_line | sub("^ +"; ""))"],
  request, breadcrumbs: [.breadcrumbs[]?.message]
}'
pause

step "6. Secrets never leave the app"
ID=$(issue_id "login failed")
run curl $API/api/1/issues/$ID/events
echo "  The code sent password=\"hunter2\". What the server stored:"
api "/api/1/issues/$ID/events" | jq '.[0].payload.extra'

step "Done: open the dashboard"
IP=$(curl -fs -m 3 https://ifconfig.me 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')
echo "  On this machine:  http://127.0.0.1:8000/dashboard#project=1&key=$KEY"
[[ -n "$IP" ]] && echo "  From your laptop: http://$IP:8000/dashboard#project=1&key=$KEY"
echo "  Tests: pytest -q    Benchmark: python bench/worker_bench.py --events 3000"
