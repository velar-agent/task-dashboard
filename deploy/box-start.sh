#!/usr/bin/env bash
# box에서 로컬 서버와 푸시 루프를 idempotent하게 띄운다.
# 이미 떠 있는 프로세스는 건드리지 않고 없는 것만 setsid nohup으로 시작한다.
# 푸시 루프는 HTTPS 대상(google-dev/google-prod)에 전송합니다.
# CONNECT_IP가 있으면 TCP 연결만 해당 IP로 고정하고 SNI/인증서 검증은 원래 호스트명으로 수행합니다.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$REPO_ROOT"

CONNECT_IP="${CONNECT_IP:-35.252.213.245}"

# (a) 로컬 서버 python3 server.py (PORT=8787)
if ! pgrep -f "python3 server.py" >/dev/null; then
  PORT=8787 setsid nohup python3 server.py >>/workspace/task-dashboard.out 2>&1 < /dev/null &
fi

# (b) 푸시 루프 dev (HTTPS 대상, CONNECT_IP 핀)
if ! pgrep -f "push-to-worker.py.*google-dev" >/dev/null; then
  CONNECT_IP="$CONNECT_IP" setsid nohup python3 deploy/push-to-worker.py --target google-dev --loop 15 >>/workspace/task-push-google-dev.log 2>&1 < /dev/null &
fi

# (b) 푸시 루프 prod (HTTPS 대상, CONNECT_IP 핀)
if ! pgrep -f "push-to-worker.py.*google-prod" >/dev/null; then
  CONNECT_IP="$CONNECT_IP" setsid nohup python3 deploy/push-to-worker.py --target google-prod --loop 15 >>/workspace/task-push-google-prod.log 2>&1 < /dev/null &
fi
