#!/usr/bin/env bash
# GCE의 시스템 cron에서 매일 자정에 개발 Worker의 코드를 승격한다.
set -euo pipefail
export TZ=Asia/Seoul LC_ALL=C.UTF-8
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
repo=${TASK_DASHBOARD_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
state=${TASK_DASHBOARD_DEPLOY_STATE:-$HOME/.local/state/task-dashboard-deploy}
token_file=${TASK_DASHBOARD_DEPLOY_TOKEN_FILE:-$HOME/.config/usage-deploy/cf-token}
for arg in "$@"; do
  case "$arg" in
    --dry-run|--force) ;;
    *) echo "사용법: nightly-promote.sh [--dry-run] [--force]" >&2; exit 10 ;;
  esac
done

umask 077
mkdir -p "$state"
chmod 700 "$state"
log="$state/$(date +%F).log"
exec 9>"$state/lock"
if ! flock -n 9; then
  echo "$(date '+%F %T %Z') SKIP 이미 승격을 실행 중입니다." >> "$log"
  exit 9
fi
exec >>"$log" 2>&1

finish() {
  local rc=$1
  echo "=== $(date '+%F %T %Z') 완료 exit=$rc ==="
  # 독자는 완성된 JSON만 읽는다. 같은 상태 디렉터리에 쓰는 러너는 락으로 직렬화한다.
  python3 - "$state/last-run.json" "$(date '+%FT%T%z')" "$rc" "$options" "$log" <<'PY'
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
options="$*"
trap 'finish "$?"' EXIT
echo "=== $(date '+%F %T %Z') 자정 승격 시작 $options ==="
find "$state" -maxdepth 1 -type f -name '20??-??-??.log' -mtime +59 -delete
cd "$repo"
clean_main() {
  local branch changes
  branch=$(git rev-parse --abbrev-ref HEAD) || return 1
  changes=$(git status --porcelain --untracked-files=normal) || return 1
  if [[ "$branch" != main || -n "$changes" ]]; then
    echo "main 브랜치의 깨끗한 작업 트리가 필요합니다."
    return 1
  fi
}
clean_main
if ! git pull --ff-only; then
  echo "git pull --ff-only 실패"
  exit 1
fi
head=$(git rev-parse --verify HEAD) || exit 1
remote_head=$(git rev-parse --verify origin/main) || exit 1
if [[ "$head" != "$remote_head" ]]; then
  echo "HEAD가 origin/main과 다릅니다."
  exit 1
fi
echo "HEAD $head (= origin/main)"
promote=worker/scripts/promote-dev-to-prod.py
python3 "$promote" precheck || exit 1
clean_main
if [[ "$(git rev-parse --verify HEAD)" != "$head" || "$(git rev-parse --verify origin/main)" != "$head" ]]; then
  echo "점검 중 HEAD 또는 origin/main이 변경되었습니다."
  exit 1
fi
python3 "$promote" deploy --token-file "$token_file" "$@"
