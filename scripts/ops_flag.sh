#!/usr/bin/env bash
# Control board for runtime flags (src/control/db.py RUNTIME_FLAG_DEFINITIONS)
# exposed via GET/PUT/DELETE /api/flags on the gateway control API.
#
# Token resolution order: $DASHBOARD_TOKEN env var, else DASHBOARD_TOKEN (or
# WORKER_TOKEN fallback) read from .env in the repo root. Requires `jq`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:9003}"
SELF="$(basename "${BASH_SOURCE[0]}")"

usage() {
    cat <<EOF
Control board for the gateway's runtime flag registry (GET/PUT/DELETE /api/flags).

Usage:
  $SELF list                    show every flag as a table (value, source, scope, writable)
  $SELF explain                 like list, but with each flag's description (what it does)
  $SELF get   <FLAG_NAME>       show one flag's full detail
  $SELF on    <FLAG_NAME>       set flag to true
  $SELF off   <FLAG_NAME>       set flag to false
  $SELF unset <FLAG_NAME>       remove the registry override, revert to env/default
  $SELF migrate                 write every registry_writable flag's CURRENT effective
                                 value into the registry (source: env/default -> registry).
                                 No behavior change — same value, just an explicit row.
                                 Skips flags already on source=registry. Non-writable
                                 flags (bootstrap/worker-side) are reported, not touched.
  $SELF help                    show this message

Notes:
  - "startup"-scoped flags (e.g. QUOTA_COORDINATOR_ENABLED) need
    'pm2 restart ai-team-gateway' after on/off/unset to actually take effect.
  - "live"-scoped flags (e.g. CASE_CONTINUATION_ENABLED) apply immediately.
  - Token is reused automatically from \$DASHBOARD_TOKEN or the repo's .env —
    nothing to configure.
  - SOURCE column meaning (fallback order, highest wins):
      registry -> explicit DB override, set via PUT (what 'on'/'off'/'migrate' write)
      env      -> read from .env / process environment, no DB row exists
      default  -> nobody set it anywhere; this is the value hardcoded in
                  RUNTIME_FLAG_DEFINITIONS (src/control/db.py) as the fallback

Examples:
  $SELF list
  $SELF get CASE_CONTINUATION_ENABLED
  $SELF on  CASE_CONTINUATION_ENABLED
  $SELF off QUOTA_COORDINATOR_ENABLED && pm2 restart ai-team-gateway
EOF
}

require_jq() {
    command -v jq >/dev/null || { echo "error: this script requires 'jq' (sudo apt install jq)" >&2; exit 1; }
}

resolve_token() {
    if [[ -n "${DASHBOARD_TOKEN:-}" ]]; then
        echo "$DASHBOARD_TOKEN"
        return
    fi
    local env_file="$REPO_ROOT/.env"
    if [[ -f "$env_file" ]]; then
        local v
        v="$(grep -E '^DASHBOARD_TOKEN=' "$env_file" | tail -1 | cut -d= -f2-)"
        if [[ -z "$v" ]]; then
            v="$(grep -E '^WORKER_TOKEN=' "$env_file" | tail -1 | cut -d= -f2-)"
        fi
        echo "$v"
        return
    fi
    echo ""
}

cmd="${1:-}"
flag="${2:-}"

if [[ -z "$cmd" || "$cmd" == "help" || "$cmd" == "-h" || "$cmd" == "--help" ]]; then
    usage
    exit 0
fi

require_jq

TOKEN="$(resolve_token)"
if [[ -z "$TOKEN" ]]; then
    echo "error: no DASHBOARD_TOKEN found (env var or $REPO_ROOT/.env)" >&2
    exit 1
fi

api() {
    # api METHOD PATH [JSON_BODY]
    local method="$1" path="$2" body="${3:-}"
    if [[ -n "$body" ]]; then
        curl -sS -X "$method" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
            -d "$body" "$GATEWAY_URL$path"
    else
        curl -sS -X "$method" -H "Authorization: Bearer $TOKEN" "$GATEWAY_URL$path"
    fi
}

print_table() {
    # reads {"ok":true,"flags":[{flag_name,value,source,effect_scope,registry_writable,...}, ...]}
    {
        echo -e "FLAG\tVALUE\tSOURCE\tSCOPE\tWRITABLE"
        jq -r '.flags[] | [.flag_name, .value, .source, .effect_scope, .registry_writable] | @tsv'
    } | column -t -s $'\t'
}

explain() {
    # reads {"ok":true,"flags":[{flag_name,value,source,effect_scope,registry_writable,description,...}, ...]}
    jq -r '.flags[] | "\(.flag_name)  [\(.value)]  source=\(.source)  scope=\(.effect_scope)  writable=\(.registry_writable)\n  \(.description)\n"'
}

case "$cmd" in
    list)
        api GET /api/flags | print_table
        ;;
    explain)
        api GET /api/flags | explain
        ;;
    get)
        [[ -n "$flag" ]] || { echo "usage: $SELF get <FLAG_NAME>" >&2; exit 1; }
        result="$(api GET /api/flags | jq --arg f "$flag" '.flags[] | select(.flag_name == $f)')"
        if [[ -z "$result" ]]; then
            echo "unknown flag: $flag" >&2
            exit 1
        fi
        echo "$result" | jq .
        ;;
    on|off)
        [[ -n "$flag" ]] || { echo "usage: $SELF $cmd <FLAG_NAME>" >&2; exit 1; }
        val=$([[ "$cmd" == "on" ]] && echo true || echo false)
        resp="$(api PUT "/api/flags/$flag" "{\"value\": $val, \"set_by\": \"$(whoami)@$SELF\"}")"
        if [[ "$(echo "$resp" | jq -r '.ok // .detail.ok // empty')" != "true" ]]; then
            msg="$(echo "$resp" | jq -r '.detail.message // .detail.reason // "unknown error"')"
            echo "error: $msg" >&2
            exit 1
        fi
        echo "$resp" | jq .
        ;;
    unset)
        [[ -n "$flag" ]] || { echo "usage: $SELF unset <FLAG_NAME>" >&2; exit 1; }
        api DELETE "/api/flags/$flag" | jq .
        ;;
    migrate)
        current="$(api GET /api/flags)"
        writable_pending="$(echo "$current" | jq -r '.flags[] | select(.registry_writable == true and .source != "registry") | [.flag_name, .value] | @tsv')"
        skipped="$(echo "$current" | jq -r '.flags[] | select(.registry_writable == false) | .flag_name')"
        already="$(echo "$current" | jq -r '.flags[] | select(.registry_writable == true and .source == "registry") | .flag_name')"

        if [[ -n "$already" ]]; then
            echo "already in registry (skipping): $(echo "$already" | tr '\n' ' ')"
        fi
        if [[ -n "$skipped" ]]; then
            echo "not registry-writable, cannot migrate (bootstrap/worker-side, left on env/default): $(echo "$skipped" | tr '\n' ' ')"
        fi
        if [[ -z "$writable_pending" ]]; then
            echo "nothing to migrate — all writable flags already in registry."
            exit 0
        fi

        echo "migrating (value unchanged, source -> registry):"
        fail=0
        while IFS=$'\t' read -r fname fval; do
            [[ -n "$fname" ]] || continue
            resp="$(api PUT "/api/flags/$fname" "{\"value\": $fval, \"set_by\": \"$(whoami)@$SELF:migrate\"}")"
            got_val="$(echo "$resp" | jq -r '.flag.value')"
            got_src="$(echo "$resp" | jq -r '.flag.source')"
            if [[ "$got_src" == "registry" && "$got_val" == "$fval" ]]; then
                echo "  OK    $fname -> $fval (registry)"
            else
                echo "  FAIL  $fname (expected $fval, got value=$got_val source=$got_src)" >&2
                fail=1
            fi
        done <<< "$writable_pending"

        if [[ "$fail" -ne 0 ]]; then
            echo "migration completed with errors — verify with '$SELF list'" >&2
            exit 1
        fi
        echo "migration complete. Verify with '$SELF list'."
        ;;
    *)
        echo "unknown command: $cmd" >&2
        usage
        exit 1
        ;;
esac
