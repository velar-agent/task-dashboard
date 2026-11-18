#!/usr/bin/env bash
# box 에서 실행하는 배포 헬퍼: VM 체크아웃을 ff-only 로 갱신한 뒤 해당 환경의 유닛만 설치/재시작한다.
# 사용법: ./vm/deploy.sh <dev|prod>
# prod 직접 배포도 가능하지만 평소엔 자정 cron 승격(vm/promote.sh)이 담당한다.
set -euo pipefail

_env="${1:-}"
case "$_env" in
  dev|prod) ;;
  *)
    echo "사용법: $0 <dev|prod>" >&2
    exit 1
    ;;
esac

if [[ "$_env" == "dev" ]]; then
  checkout="/workspace/task-dashboard-dev"
else
  checkout="/workspace/task-dashboard"
fi

ssh gce-collector "set -e
if [[ ! -d $checkout/.git ]]; then
  git clone git@github.com:velar-agent/task-dashboard.git $checkout
fi
git -C $checkout pull --ff-only
$checkout/vm/install.sh $_env"
