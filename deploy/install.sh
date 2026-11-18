#!/usr/bin/env bash
# VM 에서 collector 계정으로 실행. 유닛 설치에만 sudo 를 써요. (idempotent)
set -euo pipefail
[[ $(id -un) == collector ]] || { echo 'collector 계정에서 실행하세요.' >&2; exit 1; }
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
install -d -m 0755 /workspace/task-status
app=task-dashboard.service
tunnel=cloudflared-task-dashboard.service
sudo install -m 0644 -t /etc/systemd/system "$app" "$tunnel"
sudo systemctl daemon-reload
sudo systemctl enable --now "$app"
sudo systemctl restart "$app"
if [[ -x /home/collector/.local/bin/cloudflared ]]; then
    sudo systemctl enable --now "$tunnel"
fi
