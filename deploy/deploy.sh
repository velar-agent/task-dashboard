#!/usr/bin/env bash
# box 에서 실행: GitHub 에 푸시한 코드를 VM 이 pull 해서 서비스를 재시작해요. (usage-dashboard 와 같은 흐름)
set -euo pipefail
ssh gce-collector 'set -e
  [[ -d /workspace/task-dashboard/.git ]] || git clone git@github.com:velar-agent/task-dashboard.git /workspace/task-dashboard
  git -C /workspace/task-dashboard pull --ff-only
  /workspace/task-dashboard/deploy/install.sh'
