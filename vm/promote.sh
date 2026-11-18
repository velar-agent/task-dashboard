#!/usr/bin/env bash
# VM 의 collector 가 cron 으로 실행하는 dev -> prod 승격 러너.
# 단순 git fast-forward 승격이다.
# 비밀 값은 읽거나 출력하지 않는다.
# 사용법: vm/promote.sh [--dry-run] [--force]
# 종료 코드: 0 성공/변경없음, 1 점검·git 실패, 3 dev≠main,
#            5 승격 후 점검 실패→롤백됨, 6 롤백 실패(수동), 7 fast-forward 불가 등 안전 점검 거부,
#            9 이미 실행 중, 10 사용법
set -u -o pipefail
export TZ=Asia/Seoul
export LC_ALL=C.UTF-8 PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

DEV_DIR="${DEV_DIR:-/workspace/task-dashboard-dev}"
PROD_DIR="${PROD_DIR:-/workspace/task-dashboard}"
STATE_DIR="${PROMOTE_STATE:-${TASK_DASHBOARD_DEPLOY_STATE:-$HOME/.local/state/task-dashboard-deploy}}"
SYSTEMCTL="${SYSTEMCTL:-sudo systemctl}"
CURL_BIN="${CURL_BIN:-curl}"
HEALTH_CMD="${HEALTH_CMD:-}"
PROMOTE_SLEEP="${PROMOTE_SLEEP:-1}"
DEV_PORT=8790
PROD_PORT=8791

dry_run=false
force=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=true ;;
    --force) force=true ;;
    *)
      echo "사용법: $0 [--dry-run] [--force]" >&2
      exit 10
      ;;
  esac
done
options="$*"

umask 077
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
LOG="$STATE_DIR/$(TZ=Asia/Seoul date +%F).log"

exec 9>"$STATE_DIR/lock"
if ! flock -n 9; then
  printf '%s SKIP 이미 승격을 실행 중입니다.\n' "$(date '+%F %T %Z')" >> "$LOG" 2>/dev/null || true
  exit 9
fi

find "$STATE_DIR" -maxdepth 1 -type f -name '20??-??-??.log' -mtime +59 -delete 2>/dev/null || true

exec >>"$LOG" 2>&1

write_status() {
  local rc="$1"
  python3 - "$STATE_DIR/last-run.json" "$(date '+%FT%T%z')" "$rc" "$options" "$LOG" <<'PY'
import json
import sys
from pathlib import Path

path, stamp, code, args, log = sys.argv[1:]
dest = Path(path)
temp = dest.with_suffix(".tmp")
temp.write_text(json.dumps(dict(time=stamp, exit=int(code), args=args, log=log)) + "\n", encoding="utf-8")
temp.replace(dest)
PY
}

finish() {
  local rc="$1"
  echo "=== $(date '+%F %T %Z') 완료 exit=$rc ==="
  write_status "$rc"
  exit "$rc"
}

run_systemctl() {
  read -ra _sc <<< "$SYSTEMCTL"
  "${_sc[@]}" "$@"
}

run_health_cmd() {
  local port="$1" repo="$2"
  read -ra _hc <<< "$HEALTH_CMD"
  "${_hc[@]}" "$port" "$repo"
}

expected_page() {
  local repo="$1"
  python3 - "$repo" <<'PY'
import sys
from pathlib import Path

repo = Path(sys.argv[1])
sys.path.insert(0, str(repo))
import server
sys.stdout.write(server.PAGE)
PY
}

health_check_once() {
  local port="$1" repo="$2"
  if [[ -n "$HEALTH_CMD" ]]; then
    run_health_cmd "$port" "$repo"
    return $?
  fi
  local base="http://127.0.0.1:${port}"
  local expect_page body_file code
  expect_page="$(expected_page "$repo")" || return 1
  body_file="$(mktemp)"
  # GET /healthz 200
  read -ra _curl <<< "$CURL_BIN"
  if ! code=$("${_curl[@]}" -s -o "$body_file" -w "%{http_code}" --max-time 5 "$base/healthz" 2>/dev/null); then
    rm -f "$body_file"
    return 1
  fi
  if [[ "$code" != "200" ]]; then rm -f "$body_file"; return 1; fi
  # GET / 200 HTML + PAGE equality
  if ! code=$("${_curl[@]}" -s -o "$body_file" -w "%{http_code}" --max-time 5 "$base/" 2>/dev/null); then
    rm -f "$body_file"
    return 1
  fi
  if [[ "$code" != "200" ]]; then rm -f "$body_file"; return 1; fi
  local served_page
  served_page="$(cat "$body_file")"
  if [[ "$served_page" != "$expect_page" ]]; then
    rm -f "$body_file"
    return 1
  fi
  # GET /api/tasks 200
  if ! code=$("${_curl[@]}" -s -o "$body_file" -w "%{http_code}" --max-time 5 "$base/api/tasks" 2>/dev/null); then
    rm -f "$body_file"
    return 1
  fi
  if [[ "$code" != "200" ]]; then rm -f "$body_file"; return 1; fi
  # PUT /api/ingest unauthenticated 401
  if ! code=$("${_curl[@]}" -s -o "$body_file" -w "%{http_code}" --max-time 5 -X PUT "$base/api/ingest" 2>/dev/null); then
    rm -f "$body_file"
    return 1
  fi
  if [[ "$code" != "401" ]]; then rm -f "$body_file"; return 1; fi
  rm -f "$body_file"
  return 0
}

health_check_retry() {
  local port="$1" repo="$2" attempts="${3:-15}"
  local i
  for ((i=1; i<=attempts; i++)); do
    if health_check_once "$port" "$repo"; then
      return 0
    fi
    if [[ "$i" -lt "$attempts" ]]; then sleep "$PROMOTE_SLEEP"; fi
  done
  return 1
}

echo "=== $(date '+%F %T %Z') 승격 시작 $options ==="

# (1) DEV git fetch origin, main·깨끗함(untracked 제외)·HEAD==origin/main
if ! git -C "$DEV_DIR" fetch origin; then
  echo "FAIL DEV git fetch origin 실패"
  finish 1
fi
dev_branch="$(git -C "$DEV_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)" || finish 1
dev_changes="$(git -C "$DEV_DIR" status --porcelain --untracked-files=no 2>/dev/null)" || finish 1
# untracked 제외하고 깨끗함: status --untracked-files=no 로만 검사한다.
if [[ "$dev_branch" != "main" || -n "$dev_changes" ]]; then
  echo "FAIL dev≠main: branch=$dev_branch dirty=$([ -n "$dev_changes" ] && echo yes || echo no). 배포하지 않아요."
  finish 3
fi
dev_head="$(git -C "$DEV_DIR" rev-parse --verify HEAD 2>/dev/null)" || finish 1
remote_head="$(git -C "$DEV_DIR" rev-parse --verify origin/main 2>/dev/null)" || finish 1
if [[ "$dev_head" != "$remote_head" ]]; then
  echo "FAIL dev HEAD($dev_head) != origin/main($remote_head). 배포하지 않아요."
  finish 3
fi
echo "DEV HEAD $dev_head (= origin/main)"

# (2) 점검: DEV unittest + dev 서비스 점검
if ! (cd "$DEV_DIR" && python3 -m unittest); then
  echo "FAIL DEV python3 -m unittest 실패"
  finish 1
fi
if ! health_check_once "$DEV_PORT" "$DEV_DIR"; then
  echo "FAIL dev 서비스 점검 실패 (127.0.0.1:$DEV_PORT)"
  finish 1
fi
echo "dev 점검 통과 (127.0.0.1:$DEV_PORT /healthz / /api/tasks /api/ingest 401 + PAGE)"

# (3) PROD HEAD == DEV HEAD 건너뜀
prod_head="$(git -C "$PROD_DIR" rev-parse --verify HEAD 2>/dev/null)" || finish 1
if [[ "$prod_head" == "$dev_head" ]]; then
  if [[ "$force" != "true" ]]; then
    echo "PROD HEAD == DEV HEAD ($dev_head). 변경 없음으로 건너뜀."
    finish 0
  fi
  echo "--force: HEAD가 같아도 계속 진행합니다."
fi

# (4) PROD 사전 점검: main·깨끗함(untracked 제외), prev 기록, fast-forward 가능
prod_branch="$(git -C "$PROD_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)" || finish 1
prod_changes="$(git -C "$PROD_DIR" status --porcelain --untracked-files=no 2>/dev/null)" || finish 1
# untracked 제외하고 깨끗함: status --untracked-files=no 로만 검사한다.
if [[ "$prod_branch" != "main" || -n "$prod_changes" ]]; then
  echo "FAIL PROD 안전 점검 거부: branch=$prod_branch dirty=$([ -n "$prod_changes" ] && echo yes || echo no). 수동 확인이 필요합니다."
  finish 7
fi
prev="$(git -C "$PROD_DIR" rev-parse --verify HEAD 2>/dev/null)" || finish 1
if ! git -C "$PROD_DIR" fetch origin 2>/dev/null; then
  echo "FAIL PROD git fetch origin 실패"
  finish 1
fi
if ! git -C "$PROD_DIR" merge-base --is-ancestor "$prev" "$dev_head" 2>/dev/null; then
  echo "FAIL fast-forward 불가: PROD($prev)가 DEV($dev_head)의 조상이 아닙니다. 수동 확인이 필요합니다."
  finish 7
fi
echo "PROD precheck 통과 prev=$prev dev=$dev_head (ff 가능)"

if [[ "$dry_run" == "true" ]]; then
  echo "DRY-RUN 예정: PROD git fetch origin"
  echo "DRY-RUN 예정: PROD git merge --ff-only $dev_head"
  echo "DRY-RUN 예정: systemctl restart task-dashboard"
  echo "DRY-RUN 예정: prod 점검 (127.0.0.1:$PROD_PORT 재시도 포함)"
  finish 0
fi

# (5) PROD fetch, merge --ff-only, restart, 점검 (재시도 최대 약 15초; 페이지 비교 포함)
rollback_needed=false
if ! git -C "$PROD_DIR" fetch origin; then
  echo "FAIL PROD git fetch origin 실패"
  finish 1
fi
if ! git -C "$PROD_DIR" merge --ff-only "$dev_head"; then
  echo "FAIL PROD git merge --ff-only $dev_head 실패"
  rollback_needed=true
else
  if ! run_systemctl restart task-dashboard; then
    echo "FAIL PROD systemctl restart 실패"
    rollback_needed=true
  elif ! health_check_retry "$PROD_PORT" "$PROD_DIR" 15; then
    echo "FAIL prod 점검 실패 (127.0.0.1:$PROD_PORT)"
    rollback_needed=true
  else
    echo "승격 성공 prod 점검 통과"
    finish 0
  fi
fi

# (6) 실패 시 롤백: reset --hard prev, 재시작, 다시 점검
if [[ "$rollback_needed" == "true" ]]; then
  echo "롤백 시도 prev=$prev"
  if ! git -C "$PROD_DIR" reset --hard "$prev"; then
    echo "FAIL 롤백 git reset 실패. 수동 확인이 필요합니다."
    finish 6
  fi
  if ! run_systemctl restart task-dashboard; then
    echo "FAIL 롤백 systemctl restart 실패. 수동 확인이 필요합니다."
    finish 6
  fi
  if health_check_retry "$PROD_PORT" "$PROD_DIR" 15; then
    echo "롤백 점검 통과"
    finish 5
  else
    echo "FAIL 롤백 후 점검 실패. 수동 확인이 필요합니다."
    finish 6
  fi
fi

finish 1
