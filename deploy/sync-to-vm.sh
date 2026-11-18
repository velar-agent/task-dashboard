#!/usr/bin/env bash
# box 의 tasks.jsonl 을 VM 으로 복사해요(내용이 바뀐 때만, VM 에서 원자적으로 교체).
# 상태 파일은 box 가 원본이고 VM 은 사본이에요. box 에서 루프로 돌려요: sync-to-vm.sh --loop [초]
set -uo pipefail
SRC=${TASKS_FILE:-/workspace/task-status/tasks.jsonl}
STATE=${STATE:-/tmp/task-sync-$(id -u).sha}
DEST=/workspace/task-status/tasks.jsonl
sync_once() {
    [[ -f "$SRC" ]] || { echo "동기화 원본이 없습니다: $SRC" >&2; return 1; }
    local sum
    sum=$(sha256sum "$SRC" | cut -d' ' -f1)
    [[ "$sum" == "$(cat "$STATE" 2>/dev/null)" ]] && return 0
    ssh -o BatchMode=yes -o ConnectTimeout=15 gce-collector \
        "mkdir -p /workspace/task-status && cat > $DEST.tmp && mv $DEST.tmp $DEST" < "$SRC" \
        && echo "$sum" > "$STATE"
}
if [[ ${1:-} == --loop ]]; then
    while true; do sync_once || echo "sync 실패 $(date '+%F %T')" >&2; sleep "${2:-15}"; done
else
    sync_once
fi
