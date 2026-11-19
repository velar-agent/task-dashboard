# 봇 작업 현황

공용 JSONL 로그에 보고하는 모든 봇의 작업을 상태(진행 중, 막힘, 완료) 순으로, 그룹 안에서는 task_id 자연 정렬 순으로 보여주는 한국어 대시보드입니다. 단, 완료 작업은 항상 목록 뒤에 표시합니다. Python 3 표준 라이브러리로 동작하며 화면에 필요한 HTML, CSS, JavaScript는 `server.py`에 들어 있습니다.

## 실행

```sh
cd /workspace/task-dashboard
python3 server.py
```

기본 바인딩은 `0.0.0.0:8787`입니다. 실행한 컴퓨터에서는 [작업 현황](http://localhost:8787/)을 열고, 다른 기기에서는 주소의 `localhost`를 서버의 호스트 이름이나 IP로 바꾸세요.

| 환경변수 | 기본값 | 용도 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 바인딩 주소 |
| `PORT` | `8787` | HTTP 포트 |
| `TASKS_FILE` | `/workspace/task-status/tasks.jsonl` | 읽을 이벤트 파일 |

봇 드롭다운과 상태 탭을 함께 써서 작업을 걸러 볼 수 있습니다. 탭의 개수는 선택한 봇을 기준으로 계산하며, 이름이 빈 봇은 `(미지정)`으로 표시합니다. 30초마다 목록을 갱신할 때 선택한 봇이 계속 있으면 선택을 유지하고, 사라지면 전체 봇을 표시합니다. 행이나 작업 제목을 누르면 이벤트 이력을 펼칠 수 있습니다. 표의 시각은 모두 한국 시각(KST)입니다.
이벤트 이력을 펼치면 제목 아래에 task_id, 제목, 봇, 현재 단계, 상태, 시작, 마지막 업데이트, 이벤트 수와 최신 메모/다음 할 일 요약이 표시됩니다.

## 이벤트 키워드 검색

필터 바 위의 검색 상자(`#search`)로 이벤트 키워드를 검색합니다. 플레이스홀더는 `이벤트 키워드 검색 (공백으로 여러 단어, AND)`이며, 공백으로 나눈 모든 단어가 하나의 이벤트 안에 있어야 합니다(대소문자 무시, NFC 정규화). 검색 대상은 각 작업의 `events`만이며, 각 이벤트의 `task_id`, `title`, `bot`, `stage`, `note`, `next`, 표시 시각을 합친 텍스트입니다. 현재 작업 필드만으로 매칭하지 않으며, 최소 하나의 일치 이벤트가 있는 작업만 표시합니다. 봇 필터와 상태 필터와 함께 동작하고, 상태 탭 개수는 검색과 봇 필터를 반영합니다. 완료를 뒤로 두는 순서와 열 표시·순서 설정은 그대로 유지됩니다.

검색 중에는 일치한 작업 아래에 일치 이벤트만 `일치한 이벤트 N개 (전체 M개)` 제목으로 자동 표시하고, 행을 눌러 전체 이력을 펼치면 일치 이벤트에 왼쪽 강조와 `<mark>` 하이라이트가 표시됩니다. 행의 작업 ID, 제목, 봇, 단계, 메모, 다음 할 일, 시각과 이벤트 필드에도 일치 부분이 `<mark>`로 표시되며, 텍스트 노드로 안전하게 렌더링합니다. 검색 상태는 `검색어 "xxx": N개 작업, M개 이벤트 일치`(현재 표시된 작업 기준)로 안내하고, 결과가 없으면 `일치하는 이벤트가 없습니다.`를 표시합니다. 입력은 약 200ms 디바운스로 적용되며, Enter와 지우기 버튼(×)은 즉시 적용합니다. Escape 키는 검색어를 지웁니다. 질의는 URL의 `?q=...`로 유지되며(다른 파라미터 보존, 새로고침 없이 `history.replaceState`), 30초 자동 갱신 후에도 검색어와 포커스가 유지됩니다. 열 설정의 `localStorage` 동작은 변경하지 않았습니다. `vm/server.py`는 `server.PAGE`를 재사용하므로 별도 변경이 필요 없습니다.

## 이벤트 기록과 API

공용 디렉터리의 헬퍼로 이벤트를 추가합니다. 첫 이벤트에는 제목이 필요합니다.

```sh
/workspace/task-status/report.sh --task-id build-check --stage 계획 --title "빌드 점검"
/workspace/task-status/report.sh --task-id build-check --stage 점검 --note "빌드 성공" --next "결과 공유"
```

| 요청 | 응답 |
| --- | --- |
| `GET /api/tasks` | 공용 로그의 전체 작업 배열 `tasks`와 생성 시각 `generated_at` |
| `GET /api/tasks/<task_id>` | 해당 작업 객체와 그 안의 전체 `events` 배열. 없는 ID는 HTTP 404 |
| `GET /healthz` | HTTP 200과 `{"status":"ok"}` |

이벤트 필드와 헬퍼 옵션은 공용 로그 문서에 있습니다. [공용 로그 사용법](/workspace/task-status/README.md)

## 구글 VM(duckdns) dev+prod

구글 VM(Ubuntu 24.04, python3.12, 메모리 1GB, rg 없음)에서 task-dashboard를 nginx 뒤에 두고 dev와 prod 두 서비스를 `https://...` (443, Let's Encrypt)로 서비스합니다. VM 서버는 파이썬 표준 라이브러리만으로 동일한 API를 제공합니다. nginx는 `/etc/nginx/sites-available/task-dashboard` 한 파일에 `server_name`이 다른 server 블록 두 개를 두고, 둘 다 포트 80에서만 받고 `return 301 https://$host$request_uri;`로 443으로 리다이렉트만 합니다(일반 HTTP는 앱으로 가지 않습니다). 443(TLS) 설정은 Engineering이 관리하는 별도 파일 `/etc/nginx/sites-available/task-dashboard-https`에 두며(Let's Encrypt), 본 파일(`/etc/nginx/sites-available/task-dashboard`)은 80->443 리다이렉트만 담당합니다. `default_server`는 쓰지 않으며 `usage-dashboard` 파일은 참조하지 않습니다.

### 구조와 포트

- `vm/server.py`: 표준 라이브러리 전용 서버. 같은 저장소의 `server.py`에서 `PAGE`를 import 해 동일 화면을 서빙하며 `ThreadingHTTPServer`로 `HOST`(기본 `127.0.0.1`) `PORT`(유닛에서 주입)에 뜨고, 스냅샷은 `DATA_FILE` 한 파일에 임시파일+`os.replace`로 원자 저장합니다.
- `vm/task-dashboard-dev.service`: dev 유닛(`User=collector`, `WorkingDirectory=/workspace/task-dashboard-dev`). `EnvironmentFile=/home/collector/.config/task-dashboard-dev/env`로 `INGEST_KEY`를 읽고 `HOST=127.0.0.1` `PORT=8790` `DATA_FILE=/var/lib/task-dashboard-dev/snapshot.json` `PYTHONDONTWRITEBYTECODE=1`, `StateDirectory=task-dashboard-dev`, `ExecStart=/usr/bin/python3 vm/server.py`, `Restart=always`로 재부팅 후에도 자동 시작합니다.
- `vm/task-dashboard.service`: prod 유닛(`User=collector`, `WorkingDirectory=/workspace/task-dashboard`). `EnvironmentFile=/home/collector/.config/task-dashboard/env`, `HOST=127.0.0.1` `PORT=8791` `DATA_FILE=/var/lib/task-dashboard/snapshot.json` `PYTHONDONTWRITEBYTECODE=1`, `StateDirectory=task-dashboard`, `ExecStart=/usr/bin/python3 vm/server.py`로 dev와 경로·환경파일·`StateDirectory`·데이터 파일·포트를 분리합니다. 두 서비스는 서로 다른 `INGEST_KEY`, 별도 코드 체크아웃, 별도 데이터 파일을 씁니다. `8787`/`8788`은 쓰지 않습니다.
- `vm/nginx-task-dashboard.conf`: `server_name task-dashboard-dev.velar-agent.duckdns.org`와 `server_name task-dashboard.velar-agent.duckdns.org` 두 server 블록. 둘 다 `listen 80;`, `listen [::]:80;`만 열고 `return 301 https://$host$request_uri;`로 443으로 리다이렉트만 합니다(프록시가 없어 일반 HTTP는 앱으로 가지 않습니다). `default_server`는 없습니다. 443(TLS) 설정은 Engineering이 관리하는 별도 파일 `/etc/nginx/sites-available/task-dashboard-https`(Let's Encrypt)에 두며, 본 파일(`vm/nginx-task-dashboard.conf`)은 80->443 리다이렉트만 담당합니다.
- `vm/install.sh`: collector로 실행하는 idempotent 설치. 인자 `dev` 또는 `prod`(필수)를 받아 스크립트가 있는 저장소 루트가 기대 경로와 맞는지 확인한 뒤 해당 유닛만 설치/재시작하고, nginx 공유 파일 하나를 설치합니다. sudo는 유닛과 nginx 설치에만 씁니다.
- `vm/deploy.sh`: box에서 실행하는 배포 헬퍼.
- `vm/promote.sh`: VM의 collector가 cron으로 실행하는 dev→prod 승격 러너.
- `vm/cron/task-dashboard-nightly`: VM에 설치하는 cron 파일.

| 항목 | dev | prod |
| --- | --- | --- |
| 공개 주소 | `https://task-dashboard-dev.velar-agent.duckdns.org` (443, Let's Encrypt) | `https://task-dashboard.velar-agent.duckdns.org` (443, Let's Encrypt) |
| VM 내 서버 | `127.0.0.1:8790` | `127.0.0.1:8791` |
| systemd 유닛 | `task-dashboard-dev` | `task-dashboard` |
| 체크아웃 | `/workspace/task-dashboard-dev` | `/workspace/task-dashboard` |
| 데이터 파일 | `/var/lib/task-dashboard-dev/snapshot.json` | `/var/lib/task-dashboard/snapshot.json` |
| VM 키 파일 | `~/.config/task-dashboard-dev/env` (`INGEST_KEY=...` 한 줄, 권한 `600`) | `~/.config/task-dashboard/env` (`INGEST_KEY=...` 한 줄, 권한 `600`) |
| box 키 파일 | `~/.config/task-dashboard/ingest-key-google-dev` | `~/.config/task-dashboard/ingest-key-google-prod` |
| box 상태 해시 | `/tmp/task-push-google-dev-$UID.sha` | `/tmp/task-push-google-prod-$UID.sha` |

### 키와 설치

키는 박스에서 직접 VM에 설치합니다. 설치 스크립트는 환경 파일이 없으면 만들지 않고 안내한 뒤 실패합니다.

```sh
# VM에서 (collector로)
install -d -m 700 /home/collector/.config/task-dashboard-dev
printf 'INGEST_KEY=%s\n' '여기에-키를-입력' > /home/collector/.config/task-dashboard-dev/env
chmod 600 /home/collector/.config/task-dashboard-dev/env
install -d -m 700 /home/collector/.config/task-dashboard
printf 'INGEST_KEY=%s\n' '여기에-키를-입력' > /home/collector/.config/task-dashboard/env
chmod 600 /home/collector/.config/task-dashboard/env

cd /workspace/task-dashboard-dev && ./vm/install.sh dev
cd /workspace/task-dashboard && ./vm/install.sh prod
```

`vm/install.sh <dev|prod>`는 체크아웃(`dev`→`/workspace/task-dashboard-dev`, `prod`→`/workspace/task-dashboard`)과 일치할 때 해당 유닛만 `/etc/systemd/system/<unit>.service`에 설치하고 `daemon-reload`, `enable --now`, 재시작합니다. nginx는 `/etc/nginx/sites-available/task-dashboard`로 복사해 `/etc/nginx/sites-enabled/task-dashboard` 심볼릭 링크를 만든 뒤 `sudo nginx -t`를 통과해야만 `reload nginx`합니다. 이 nginx 파일은 80->443 리다이렉트만 담당하고 443 TLS 블록은 Engineering이 관리하는 별도 파일에 둡니다. 실패하면 링크를 제거하고 비0으로 끝납니다. 다른 nginx 사이트 파일은 건드리지 않습니다.

재시작과 로그:

```sh
sudo systemctl restart task-dashboard-dev
systemctl status task-dashboard-dev
journalctl -u task-dashboard-dev -n 40
sudo systemctl restart task-dashboard
systemctl status task-dashboard
journalctl -u task-dashboard -n 40
sudo nginx -t && sudo systemctl reload nginx
journalctl -u nginx -n 40
curl -i http://127.0.0.1:8790/healthz
curl -i http://127.0.0.1:8791/healthz
curl -i https://task-dashboard-dev.velar-agent.duckdns.org/healthz
curl -i https://task-dashboard.velar-agent.duckdns.org/healthz
# 공개 80번은 443으로 301 리다이렉트만 한다 (일반 HTTP는 앱으로 가지 않는다)
```

### 배포 헬퍼 (`vm/deploy.sh`)

box에서 실행하며 `dev` 또는 `prod`를 받습니다. `ssh gce-collector`로 해당 체크아웃을 `git pull --ff-only`한 뒤 `vm/install.sh <env>`를 실행합니다. 체크아웃이 없으면 dev는 `git clone git@github.com:velar-agent/task-dashboard.git`으로 생성합니다.

```sh
./vm/deploy.sh dev    # VM의 /workspace/task-dashboard-dev 갱신 + task-dashboard-dev 재시작
./vm/deploy.sh prod   # VM의 /workspace/task-dashboard 갱신 + task-dashboard 재시작 (직접 배포도 가능하지만 평소엔 자정 승격이 담당)
```

### 자정 승격 (`vm/promote.sh`)

VM의 `collector`가 cron으로 실행하며, 비밀 값을 읽거나 출력하지 않습니다. `TZ=Asia/Seoul`이며 상태 디렉터리 `~/.local/state/task-dashboard-deploy`에 일자별 로그(60일 보관)와 `last-run.json`을 남깁니다. `flock` 락으로 겹침을 막습니다. `dev`는 `/workspace/task-dashboard-dev`, `prod`는 `/workspace/task-dashboard`입니다. `SYSTEMCTL`, `HEALTH_CMD` 환경변수로 systemctl·curl 호출을 가짜 명령으로 대체할 수 있게 설계했습니다.

순서:

1. DEV에서 `git fetch origin`, 브랜치 `main`·깨끗한 작업 트리(untracked 제외 무관 시 명시, `git status --porcelain --untracked-files=no`로만 검사)·`HEAD`가 `origin/main`과 같은지 확인합니다. 다르면 종료 3(`dev≠main`)이며 아무것도 올리지 않습니다.
2. 점검: `python3 -m unittest`(DEV), dev 서비스(`127.0.0.1:8790`) `/healthz` 200·`/` 200 HTML·`/api/tasks` 200·인증 없는 `PUT /api/ingest` 401, 그리고 dev가 실제로 서비스 중인 페이지가 DEV의 `server.PAGE`와 같은지 확인합니다. 실패는 종료 1입니다.
3. PROD의 `HEAD`가 DEV `HEAD`와 같으면 건너뜀(0, `--force`면 계속).
4. PROD 사전 점검: 브랜치 `main`·깨끗함(untracked 제외), 기존 `HEAD(prev)` 기록, DEV `HEAD`가 PROD `HEAD`의 후손(fast-forward 가능)인지 확인합니다. 아니면 종료 7(수동 확인).
5. PROD에서 `git fetch origin` 후 `git merge --ff-only <DEV HEAD>`, `sudo systemctl restart task-dashboard`, 점검(`127.0.0.1:8791` 같은 항목, 재시도 최대 약 15초; 페이지 비교 포함).
6. 실패 시 `git reset --hard <prev>`, 재시작, 다시 점검: 통과하면 종료 5, 아니면 종료 6. 성공 0.

```sh
/workspace/task-dashboard-dev/vm/promote.sh
/workspace/task-dashboard-dev/vm/promote.sh --dry-run   # (4)까지만 수행하고 (5)(6)은 "예정"만 출력
/workspace/task-dashboard-dev/vm/promote.sh --force     # HEAD가 같아도 계속 진행
```

| 종료 코드 | 의미 |
| --- | --- |
| 0 | 성공/변경없음 |
| 1 | 점검·git 실패 |
| 3 | dev≠main |
| 5 | 승격 후 점검 실패→롤백됨 |
| 6 | 롤백 실패(수동) |
| 7 | fast-forward 불가 등 안전 점검 거부 |
| 9 | 이미 실행 중 |
| 10 | 사용법 |

### cron

`vm/cron/task-dashboard-nightly`는 VM의 `/etc/cron.d/task-dashboard-nightly`로 설치합니다. `usage-dashboard`의 cron과 별개 파일이며 락과 상태 디렉터리가 독립적입니다.

```
CRON_TZ=Asia/Seoul
0 0 * * * collector /workspace/task-dashboard-dev/vm/promote.sh
```

VM에서 확인:

```sh
cat /etc/cron.d/task-dashboard-nightly
state="$HOME/.local/state/task-dashboard-deploy"
cat "$state/last-run.json"
tail -n 40 "$state/$(TZ=Asia/Seoul date +%F).log"
```

### 푸시

box에서 계속 동기화하려면 푸시 스크립트의 google-dev/google-prod 대상을 씁니다. 같은 `tasks`면 전송하지 않습니다.

```sh
./deploy/push-to-worker.py --target google-dev --loop 15
./deploy/push-to-worker.py --target google-prod --loop 15
```

박스 DNS가 가짜 `198.18.x`를 돌려주면 `CONNECT_IP`에 실제 VM IP를 넣어 그 IP로 TCP 연결을 고정하고, SNI와 인증서 검증, `Host` 헤더는 원래 호스트명으로 보냅니다(대상은 HTTPS이며 인증서를 검증합니다).

```sh
CONNECT_IP=203.0.113.10 ./deploy/push-to-worker.py --target google-dev --loop 15
CONNECT_IP=203.0.113.10 ./deploy/push-to-worker.py --target google-prod --loop 15
# 또는 WORKER_URL 재지정:
# CONNECT_IP=203.0.113.10 WORKER_URL=https://task-dashboard-dev.velar-agent.duckdns.org \
#   ./deploy/push-to-worker.py --target google-dev --loop 15
# CONNECT_IP=203.0.113.10 WORKER_URL=https://task-dashboard.velar-agent.duckdns.org \
#   ./deploy/push-to-worker.py --target google-prod --loop 15
```

박스에서는 루트 데몬이 없고 부팅 훅도 없으므로 재시작 뒤에는 `deploy/box-start.sh`를 실행해야 로컬 서버와 푸시 루프가 다시 뜹니다. 스크립트는 이미 떠 있는 경우 건드리지 않고 없는 것만 `setsid nohup`으로 띄우며, 로그는 `/workspace/task-dashboard.out`, `/workspace/task-push-google-dev.log`, `/workspace/task-push-google-prod.log`에 남깁니다. `CONNECT_IP`는 환경변수 또는 기본 `35.252.213.245`를 씁니다. 필요하면 아래와 같이 `~/.profile`에서 호출하세요.

```sh
# ~/.profile 예시
/workspace/task-dashboard/deploy/box-start.sh
```

### HTTPS 전송

google-dev/google-prod 대상은 HTTPS(`https://...`)로 전송하고 서버 인증서를 검증합니다. `CONNECT_IP`가 있으면 TCP 연결만 해당 IP로 고정하고, SNI와 인증서 검증, `Host` 헤더는 원래 호스트명을 씁니다. VM의 443(TLS) 설정은 Engineering이 관리하는 별도 파일 `/etc/nginx/sites-available/task-dashboard-https`(Let's Encrypt)에 두며, `/etc/nginx/sites-available/task-dashboard`의 80번은 443으로 301 리다이렉트만 합니다(일반 HTTP는 앱으로 가지 않습니다).

## 테스트

```sh
cd /workspace/task-dashboard
python3 -m unittest
```

푸시 테스트는 루프백 주소에 소스 서버와 가짜 서버를 띄웁니다. 화면 테스트는 headless Chrome 또는 Chromium에서 로컬 페이지를 열고, 브라우저가 없으면 건너뜁니다.

