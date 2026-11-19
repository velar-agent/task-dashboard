#!/usr/bin/env bash
# VM 에서 collector 계정으로 실행하는 idempotent 설치.
# 사용법: ./vm/install.sh <dev|prod>
# sudo 는 유닛/systemd 와 nginx 설치에만 쓴다. 비밀 값은 출력하지 않는다.
set -euo pipefail

_env="${1:-}"
case "$_env" in
  dev|prod) ;;
  *)
    echo "사용법: $0 <dev|prod>" >&2
    exit 1
    ;;
esac

if [[ "$(id -un)" != "collector" ]]; then
  echo 'collector 계정에서 실행하세요.' >&2
  exit 1
fi

# 이 스크립트(vm/)가 있는 저장소 루트가 기대 경로와 맞는지 확인한다.
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
if [[ "$_env" == "dev" ]]; then
  expected="/workspace/task-dashboard-dev"
  env_file="/home/collector/.config/task-dashboard-dev/env"
  service_name="task-dashboard-dev"
  service_src="$script_dir/task-dashboard-dev.service"
else
  expected="/workspace/task-dashboard"
  env_file="/home/collector/.config/task-dashboard/env"
  service_name="task-dashboard"
  service_src="$script_dir/task-dashboard.service"
fi
if [[ "$repo_root" != "$expected" ]]; then
  echo "체크아웃 경로가 기대와 다릅니다: $repo_root (기대: $expected, 환경: $_env)" >&2
  exit 1
fi

nginx_src="$script_dir/nginx-task-dashboard.conf"

if [[ ! -f "$env_file" ]]; then
  cat >&2 <<EOF
환경 파일이 없습니다: $env_file
박스에서 먼저 키 파일을 설치하세요 (스크립트는 만들지 않습니다). 예:
  install -d -m 700 $(dirname -- "$env_file")
  printf 'INGEST_KEY=%s\n' '여기에-키를-입력' > $env_file
  chmod 600 $env_file
EOF
  exit 1
fi

for src in "$service_src" "$nginx_src"; do
  if [[ ! -f "$src" ]]; then
    echo "필요한 파일을 찾을 수 없습니다: $src" >&2
    exit 1
  fi
done

# systemd 유닛 설치 (해당 환경만)
sudo install -m 0644 "$service_src" /etc/systemd/system/${service_name}.service
sudo systemctl daemon-reload
sudo systemctl enable ${service_name}.service
sudo systemctl restart ${service_name}.service

# nginx: 이 사이트 파일(80->443 301 리다이렉트 전용)만 건드리고 다른 사이트는 절대 건드리지 않는다.
# 443 TLS 블록은 Engineering이 관리하는 /etc/nginx/sites-available/task-dashboard-https에 별도로 둔다. 일반 HTTP는 앱으로 가지 않는다.
sudo install -m 0644 "$nginx_src" /etc/nginx/sites-available/task-dashboard
sudo ln -sf /etc/nginx/sites-available/task-dashboard /etc/nginx/sites-enabled/task-dashboard
if ! sudo nginx -t; then
  sudo rm -f /etc/nginx/sites-enabled/task-dashboard
  echo 'nginx 설정 검증에 실패하여 링크를 제거했습니다.' >&2
  exit 1
fi
sudo systemctl reload nginx
