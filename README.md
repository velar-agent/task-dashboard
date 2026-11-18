# 봇 작업 현황

공용 JSONL 로그에 보고하는 모든 봇의 작업을 최근 업데이트 순으로 보여주는 한국어 대시보드입니다. Python 3 표준 라이브러리로 동작하며 화면에 필요한 HTML, CSS, JavaScript는 `server.py`에 들어 있습니다.

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

## Cloudflare Worker

box의 로컬 서버가 JSONL 이벤트를 작업별로 접으면 푸시 스크립트가 `/api/tasks`에서 이벤트 이력을 포함한 스냅샷을 읽습니다. 스크립트가 `PUT /api/ingest`로 전송하면 Worker의 Durable Object(`TaskStore`)가 최신 스냅샷을 저장합니다. 브라우저는 `workers.dev` 주소에서 대시보드를 열고 같은 주소의 `/api/tasks`에서 목록을 읽습니다.

## 개발/운영 환경과 배포 플로우

| 환경 | Worker | 공개 주소 | `STORE` 바인딩 | `INGEST_KEY`에 대응하는 box 키 파일 |
| --- | --- | --- | --- | --- |
| 운영 (기본) | `task-dashboard` | [운영 대시보드](https://task-dashboard.velar-agent.workers.dev) | 운영 Worker 소유의 `TaskStore` | `~/.config/task-dashboard/ingest-key` |
| 개발 (`--env dev`) | `task-dashboard-dev` | [개발 대시보드](https://task-dashboard-dev.velar-agent.workers.dev) | 개발 Worker 소유의 `TaskStore` | `~/.config/task-dashboard/ingest-key-dev` |

각 Worker에는 독립된 SQLite Durable Object 네임스페이스와 서로 다른 수집 키가 있습니다. 승격은 코드만 옮깁니다. 데이터는 box에서 같은 `tasks.jsonl` 원본의 스냅샷을 각 Worker에 푸시해 채웁니다.

### box에서 개발 배포와 점검

1. 코드를 수정합니다. `server.py`의 `PAGE`를 바꿨으면 저장소 루트에서 `python3 worker/scripts/sync-html.py`로 `worker/src/html.js`도 갱신합니다.
2. agy 리뷰를 거쳐 변경을 `main`에 커밋하고 푸시합니다.
3. box에서 `./worker/scripts/deploy-dev.sh`를 실행합니다. 헬퍼는 HTML을 재생성하고 Node·Python 테스트를 통과한 뒤 `wrangler deploy --env dev`를 실행합니다. 개발 설정은 번들링을 끄고 `index.js`와 `html.js`를 원본 그대로 올립니다.
4. 개발 대시보드를 열어 변경 사항을 확인하고 `python3 worker/scripts/promote-dev-to-prod.py precheck`를 실행합니다. HTML 동기화와 두 테스트 모음, 개발의 `GET /healthz` 200 및 `{"status":"ok"}`, `GET /` 200 HTML과 로컬 화면의 일치, `GET /api/tasks` 200 JSON의 `tasks` 배열, 인증 없는 `PUT /api/ingest` 401을 점검합니다.
5. 매일 **00:00 KST**에 GCE VM의 시스템 cron이 `collector`로 `nightly-promote.sh`를 실행합니다. VM에서 받은 `main`과 실제 개발 코드가 일치하고 모든 점검이 통과하면 운영에 반영합니다.

개발 Worker를 처음 배포한 직후, 4단계 전에 `worker/`에서 `wrangler secret put INGEST_KEY --env dev`로 **개발용 키**를 프롬프트에 입력하세요. box의 `ingest-key-dev`에는 같은 개발 키를 준비하고 두 키 파일의 권한을 `600`으로 설정합니다. 운영 키를 교체할 때의 명령은 `worker/`에서 `wrangler secret put INGEST_KEY`입니다.

헬퍼의 Wrangler 실행 파일은 `WRANGLER`로 지정할 수 있습니다. 기본값은 `/workspace/wrangler-tool/node_modules/.bin/wrangler`입니다. box에서 헬퍼는 `~/.config/usage-deploy/cf-token`을 읽습니다. VM의 승격 도구는 `CLOUDFLARE_API_TOKEN`을 우선 사용하고, 그다음 `--token-file`로 지정한 파일 또는 같은 기본 경로를 읽습니다. 토큰 파일 권한은 `600`으로 설정하세요. 관리 API의 기본 계정은 `a12d19c670e84a71f5acc26df16e47f7`이며 `--account`로 바꿀 수 있습니다.

### VM의 자정 승격

러너는 전용 `flock`을 잡고 미추적 파일을 포함해 작업 트리가 깨끗한 `main`에서 `git pull --ff-only`를 수행합니다. `HEAD == origin/main`을 확인하고 `precheck`를 통과하면 `deploy`가 다음 순서로 진행합니다.

1. Workers API의 `content/v2`에서 실제 개발·운영 모듈을 읽습니다. 개발의 진입점과 모듈 이름·바이트가 로컬과 다르면 종료 3입니다.
2. 운영 트래픽이 단일 버전 100%이고 그 버전이 최근 업로드인지 확인합니다. 운영 `STORE`가 자체 `TaskStore`를 가리키고 `INGEST_KEY`가 시크릿이어야 합니다. 이 점검을 통과한 뒤 운영 코드까지 같으면 건너뜁니다.
3. Versions API에 두 모듈과 운영 DO 바인딩을 업로드합니다. 운영 시크릿은 이름과 `type=inherit`만 보내며 `bindings_inherit=strict`로 상속을 확인합니다. `observability`는 각 Worker의 Wrangler 스크립트 설정에서 관리합니다.
4. 새 버전의 바인딩을 확인한 뒤 트래픽을 100% 전환합니다. 활성 버전·코드·바인딩과 운영의 네 HTTP 경로를 다시 점검합니다. 실패하면 직전 버전으로 롤백한 뒤 활성 버전과 서비스 응답을 확인합니다.

`deploy --dry-run`은 실제 Cloudflare API에서 읽기와 안전 점검까지 수행합니다. `--force`는 코드가 같은 경우에도 배포를 진행하며 개발 코드 일치 검사와 바인딩 검사는 항상 적용합니다.

### VM에서 확인, 일시 중지와 롤백

VM에 설치할 시스템 cron 파일은 `worker/cron/crontab`, 설치 위치는 `/etc/cron.d/task-dashboard-nightly`입니다. VM에는 Python 3.11 이상, `node --test`를 지원하는 Node.js와 `flock`이 필요합니다. usage-dashboard와 실행 시각은 같고 락과 상태 디렉터리는 서로 독립적입니다.

`ssh gce-collector`로 접속한 뒤 `collector` 계정에서 확인합니다. 날짜별 로그는 60일 보관하며 `last-run.json`에는 마지막 실행 시각, 종료 코드, 인수, 로그 경로가 기록됩니다.

```sh
cd /workspace/task-dashboard
cat /etc/cron.d/task-dashboard-nightly
state="$HOME/.local/state/task-dashboard-deploy"
cat "$state/last-run.json"
tail -n 40 "$state/$(TZ=Asia/Seoul date +%F).log"
python3 worker/scripts/promote-dev-to-prod.py status
./worker/scripts/nightly-promote.sh --dry-run
```

관리자 셸에서는 다음 명령으로 이후 자정 실행을 일시 중지하거나 재개합니다.

```sh
sudo mv /etc/cron.d/task-dashboard-nightly /etc/cron.d/task-dashboard-nightly.disabled  # 중지
sudo mv /etc/cron.d/task-dashboard-nightly.disabled /etc/cron.d/task-dashboard-nightly  # 재개
```

수동 롤백 시에는 먼저 cron을 중지합니다. `status`나 당일 로그에서 정상 동작했던 **버전 ID**를 확인한 뒤 Cloudflare 대시보드의 **Workers & Pages → task-dashboard → Deployments**에서 해당 버전으로 롤백하고 트래픽 100%를 확인하세요. REST API로는 `POST /accounts/<계정>/workers/scripts/task-dashboard/deployments`에 `{"strategy":"percentage","versions":[{"version_id":"<정상 버전 ID>","percentage":100}]}`를 보냅니다. 이어서 VM 저장소 루트에서 `python3 worker/scripts/promote-dev-to-prod.py postcheck`로 운영의 네 경로를 확인합니다.

롤백 후에는 실패한 버전이 최근 업로드로 남아 다음 승격이 종료 7로 멈출 수 있습니다. 원인을 수정하고 개발에서 검증한 정상 코드로 운영 버전을 복구한 뒤, **최근 업로드 = 활성 100%**와 운영 바인딩을 확인하고 cron을 재개합니다.

| 종료 코드 | 의미 |
| --- | --- |
| 0 | 승격·점검 성공, 변경 없음 또는 dry-run 성공 |
| 1 | Git 조건, 로컬 테스트, 서비스 점검 또는 관리 API 조회 실패 |
| 2 | API 토큰을 읽을 수 없거나 파일 권한·형식 오류, 또는 운영 이외의 승격 대상 지정 |
| 3 | 개발 live 코드와 로컬 코드 불일치 |
| 4 | 새 버전 업로드·바인딩 확인·트래픽 전환 단계에서 중단 |
| 5 | 배포 후 점검 실패, 직전 버전 롤백 및 운영 재점검 성공 |
| 6 | 배포·롤백 후 활성 상태를 확인할 수 없거나 롤백 후 점검 실패. 수동 확인 필요 |
| 7 | 최근 업로드·활성 배포·필수 바인딩의 업로드 전 안전 조건 미충족 |
| 9 | 다른 자정 러너가 락을 보유 중 |
| 10 | 명령 또는 옵션 사용법 오류 |

### box에서 스냅샷 푸시

로컬 서버가 실행 중일 때 저장소 루트에서 원하는 방식으로 실행합니다.

```sh
./deploy/push-to-worker.py             # 한 번 확인
./deploy/push-to-worker.py --loop      # 15초마다 확인
./deploy/push-to-worker.py --loop 30   # 30초마다 확인
./deploy/push-to-worker.py --force     # 현재 스냅샷을 즉시 재전송
```

두 환경을 같은 원본으로 채우려면 아래 두 명령을 각각 실행합니다. 계속 동기화하려면 각 명령에 `--loop`를 붙여 별도 프로세스로 실행합니다.

```sh
./deploy/push-to-worker.py --target prod
./deploy/push-to-worker.py --target dev
```

| 환경변수 | 기본값 | 용도 |
| --- | --- | --- |
| `SOURCE_URL` | `http://127.0.0.1:8787/api/tasks` | box의 스냅샷 조회 주소 |
| `WORKER_URL` | 선택한 환경의 공개 주소 (기본 운영) | 두 환경 모두 이 변수가 우선하며 스크립트가 `/api/ingest`를 붙입니다. |
| `KEY_FILE` | 위 환경 표의 box 키 파일 | 선택한 환경에서 사용할 Bearer 키 파일을 재지정합니다. |

스크립트는 `tasks`만 해시하므로 `generated_at`만 달라지면 전송을 건너뜁니다. 전송 성공 후 SHA-256 해시는 운영에서 `/tmp/task-push-$UID.sha`, 개발에서 `/tmp/task-push-dev-$UID.sha`에 기록합니다. 각 환경의 락은 같은 이름의 `.lock` 파일입니다. 실패하면 표준 오류에 한 줄을 쓰고, 반복 모드에서는 다음 주기에 다시 확인합니다. `--loop`와 `--force`를 함께 쓰면 매 주기 전송합니다.

## 테스트

```sh
cd /workspace/task-dashboard
python3 -m unittest
node --test worker/test/
```

푸시 테스트는 루프백 주소에 소스 서버와 가짜 Worker를 띄웁니다. 화면 테스트는 headless Chrome 또는 Chromium에서 로컬 페이지를 열고, 브라우저가 없으면 건너뜁니다. Worker 테스트는 메모리 저장소를 사용하며 `worker/`에서 `npm test`로도 실행할 수 있습니다.

## Google VM 배포 (`deploy/`)
usage-dashboard 의 VM 방식과 같아요: GCE VM(`ssh gce-collector`)의 `/workspace/task-dashboard` 는 GitHub(`velar-agent/task-dashboard`, 비공개 — VM 의 읽기 전용 deploy key 로 pull) 체크아웃이고, systemd 유닛 `task-dashboard.service`(User=collector, `127.0.0.1:8788`)와 cloudflared quick tunnel 유닛 `cloudflared-task-dashboard.service` 로 노출해요.

- 재배포(box 에서, 먼저 `git push`): `deploy/deploy.sh` (VM 에서 `git pull --ff-only` → `deploy/install.sh` 로 유닛 설치·재시작)
- 상태 파일은 repo 에 없어요(`.gitignore`). box 의 `/workspace/task-status/tasks.jsonl` 이 원본이고, `deploy/sync-to-vm.sh --loop 15` 가 box 에서 15초마다 바뀐 때만 VM 의 같은 경로로 복사해요(VM 에서 `mv` 로 원자적 교체).
- 터널 URL: `ssh gce-collector 'journalctl -u cloudflared-task-dashboard -o cat | grep -o "https://[a-z0-9-]*\.trycloudflare.com" | tail -1'` (quick tunnel 이라 재시작하면 URL 이 바뀌어요)
- 로그: `journalctl -u task-dashboard`

## 구글 VM(duckdns) dev+prod

구글 VM(Ubuntu 24.04, python3.12, 메모리 1GB, rg 없음)에서 task-dashboard를 nginx 뒤에 두고 dev와 prod 두 서비스를 `http://...` (포트 80, HTTPS 없음)로 서비스합니다. VM 서버는 파이썬 표준 라이브러리만으로 기존 Worker와 같은 API를 제공합니다. nginx는 `/etc/nginx/sites-available/task-dashboard` 한 파일에 `server_name`이 다른 server 블록 두 개를 두고, 둘 다 80번으로 받아 `127.0.0.1:8790`(dev)과 `127.0.0.1:8791`(prod)로 프록시합니다. `default_server`는 쓰지 않으며 `usage-dashboard` 파일은 참조하지 않습니다.

### 구조와 포트

- `vm/server.py`: 표준 라이브러리 전용 서버. 같은 저장소의 `server.py`에서 `PAGE`를 import 해 동일 화면을 서빙하며 `worker/src/index.js`와 같은 동작·JSON 모양·상태코드·메시지를 냅니다. `ThreadingHTTPServer`로 `HOST`(기본 `127.0.0.1`) `PORT`(유닛에서 주입)에 뜨고, 스냅샷은 `DATA_FILE` 한 파일에 임시파일+`os.replace`로 원자 저장합니다.
- `vm/task-dashboard-dev.service`: dev 유닛(`User=collector`, `WorkingDirectory=/workspace/task-dashboard-dev`). `EnvironmentFile=/home/collector/.config/task-dashboard-dev/env`로 `INGEST_KEY`를 읽고 `HOST=127.0.0.1` `PORT=8790` `DATA_FILE=/var/lib/task-dashboard-dev/snapshot.json` `PYTHONDONTWRITEBYTECODE=1`, `StateDirectory=task-dashboard-dev`, `ExecStart=/usr/bin/python3 vm/server.py`, `Restart=always`로 재부팅 후에도 자동 시작합니다.
- `vm/task-dashboard.service`: prod 유닛(`User=collector`, `WorkingDirectory=/workspace/task-dashboard`). `EnvironmentFile=/home/collector/.config/task-dashboard/env`, `HOST=127.0.0.1` `PORT=8791` `DATA_FILE=/var/lib/task-dashboard/snapshot.json` `PYTHONDONTWRITEBYTECODE=1`, `StateDirectory=task-dashboard`, `ExecStart=/usr/bin/python3 vm/server.py`로 dev와 경로·환경파일·`StateDirectory`·데이터 파일·포트를 분리합니다. 두 서비스는 서로 다른 `INGEST_KEY`, 별도 코드 체크아웃, 별도 데이터 파일을 씁니다. `8787`/`8788`은 쓰지 않습니다.
- `vm/nginx-task-dashboard.conf`: `server_name task-dashboard-dev.velar-agent.duckdns.org`와 `server_name task-dashboard.velar-agent.duckdns.org` 두 server 블록. 둘 다 `listen 80;`, `listen [::]:80;`, `client_max_body_size 1m;`, `proxy_set_header Host $host;`, `proxy_set_header X-Forwarded-For $remote_addr;`, `proxy_set_header X-Forwarded-Proto http;`, `proxy_read_timeout 30s;`를 가지며 각각 `proxy_pass http://127.0.0.1:8790;`과 `proxy_pass http://127.0.0.1:8791;`로 보냅니다. `default_server`는 없습니다.
- `vm/install.sh`: collector로 실행하는 idempotent 설치. 인자 `dev` 또는 `prod`(필수)를 받아 스크립트가 있는 저장소 루트가 기대 경로와 맞는지 확인한 뒤 해당 유닛만 설치/재시작하고, nginx 공유 파일 하나를 설치합니다. sudo는 유닛과 nginx 설치에만 씁니다.
- `vm/deploy.sh`: box에서 실행하는 배포 헬퍼.
- `vm/promote.sh`: VM의 collector가 cron으로 실행하는 dev→prod 승격 러너.
- `vm/cron/task-dashboard-nightly`: VM에 설치하는 cron 파일.

| 항목 | dev | prod |
| --- | --- | --- |
| 공개 주소 | `http://task-dashboard-dev.velar-agent.duckdns.org` (80, HTTPS 없음) | `http://task-dashboard.velar-agent.duckdns.org` (80, HTTPS 없음) |
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

`vm/install.sh <dev|prod>`는 체크아웃(`dev`→`/workspace/task-dashboard-dev`, `prod`→`/workspace/task-dashboard`)과 일치할 때 해당 유닛만 `/etc/systemd/system/<unit>.service`에 설치하고 `daemon-reload`, `enable --now`, 재시작합니다. nginx는 `/etc/nginx/sites-available/task-dashboard`로 복사해 `/etc/nginx/sites-enabled/task-dashboard` 심볼릭 링크를 만든 뒤 `sudo nginx -t`를 통과해야만 `reload nginx`합니다. 실패하면 링크를 제거하고 비0으로 끝납니다. 다른 nginx 사이트 파일은 건드리지 않습니다.

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
```

### 배포 헬퍼 (`vm/deploy.sh`)

box에서 실행하며 `dev` 또는 `prod`를 받습니다. `ssh gce-collector`로 해당 체크아웃을 `git pull --ff-only`한 뒤 `vm/install.sh <env>`를 실행합니다. 체크아웃이 없으면 dev는 `git clone git@github.com:velar-agent/task-dashboard.git`으로 생성합니다.

```sh
./vm/deploy.sh dev    # VM의 /workspace/task-dashboard-dev 갱신 + task-dashboard-dev 재시작
./vm/deploy.sh prod   # VM의 /workspace/task-dashboard 갱신 + task-dashboard 재시작 (직접 배포도 가능하지만 평소엔 자정 승격이 담당)
```

### 자정 승격 (`vm/promote.sh`)

VM의 `collector`가 chron으로 실행하며, 비밀 값을 읽거나 출력하지 않습니다. `TZ=Asia/Seoul`이며 상태 디렉터리 `~/.local/state/task-dashboard-deploy`에 일자별 로그(60일 보관)와 `last-run.json`을 남깁니다. `flock` 락으로 겹침을 막습니다. `dev`는 `/workspace/task-dashboard-dev`, `prod`는 `/workspace/task-dashboard`입니다. `SYSTEMCTL`, `HEALTH_CMD` 환경변수로 systemctl·curl 호출을 가짜 명령으로 대체할 수 있게 설계했습니다.

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

box에서 계속 동기화하려면 푸시 스크립트의 google-dev/google-prod 대상을 씁니다. 같은 `tasks`면 전송하지 않습니다. 기존 `prod`/`dev` 대상도 유지합니다.

```sh
./deploy/push-to-worker.py --target google-dev --loop 15
./deploy/push-to-worker.py --target google-prod --loop 15
```

박스 DNS가 가짜 `198.18.x`를 돌려주면 `CONNECT_IP`에 실제 VM IP를 넣어 그 IP로 접속하고 `Host` 헤더는 원래 호스트로 보냅니다.

```sh
CONNECT_IP=203.0.113.10 ./deploy/push-to-worker.py --target google-dev --loop 15
CONNECT_IP=203.0.113.10 ./deploy/push-to-worker.py --target google-prod --loop 15
# 또는 WORKER_URL 재지정:
# CONNECT_IP=203.0.113.10 WORKER_URL=http://task-dashboard-dev.velar-agent.duckdns.org \
#   ./deploy/push-to-worker.py --target google-dev --loop 15
# CONNECT_IP=203.0.113.10 WORKER_URL=http://task-dashboard.velar-agent.duckdns.org \
#   ./deploy/push-to-worker.py --target google-prod --loop 15
```

### HTTPS 없음 / 평문 HTTP 키 경고

이 환경은 HTTPS가 없고 평문 HTTP(`http://...`)로 Bearer 키를 전송합니다. 네트워크 감청 시 키가 노출될 수 있습니다. VM의 nginx 80번과 box의 푸시 구간 모두 암호화되지 않습니다. 공용 네트워크에서는 쓰지 마세요.
