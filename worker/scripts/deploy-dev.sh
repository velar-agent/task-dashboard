#!/usr/bin/env bash
# box에서 두 모듈을 점검하고 개발 Worker에 배포한다.
set +x
set -euo pipefail
if (( $# )); then
  echo "사용법: deploy-dev.sh" >&2
  exit 10
fi
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
wrangler=${WRANGLER:-/workspace/wrangler-tool/node_modules/.bin/wrangler}
cd "$repo"
unset CLOUDFLARE_API_TOKEN
python3 worker/scripts/sync-html.py
node --test worker/test/
python3 -m unittest

token_file=${TASK_DASHBOARD_DEPLOY_TOKEN_FILE:-$HOME/.config/usage-deploy/cf-token}
if [[ ! -f "$token_file" || ! -r "$token_file" ]]; then
  echo "Cloudflare API 토큰 파일을 읽을 수 없습니다." >&2
  exit 2
fi
mode=$(stat -L -c '%a' "$token_file")
if (( 8#$mode & 077 )); then
  echo "API 토큰 파일 권한을 600으로 설정하세요." >&2
  exit 2
fi
CLOUDFLARE_API_TOKEN=$(<"$token_file")
if [[ -z "$CLOUDFLARE_API_TOKEN" || "$CLOUDFLARE_API_TOKEN" =~ [[:space:][:cntrl:]] ]]; then
  echo "한 줄의 API 토큰이 필요합니다." >&2
  exit 2
fi
export CLOUDFLARE_API_TOKEN WRANGLER_LOG_SANITIZE=true CLOUDFLARE_ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-a12d19c670e84a71f5acc26df16e47f7}
cd worker
exec "$wrangler" deploy --env dev
