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

### Worker 배포

box의 `~/.config/task-dashboard/ingest-key`에 전송용 키를 한 줄로 저장하고 파일 권한을 `600`으로 설정하세요. Wrangler로 배포할 Cloudflare 계정에 로그인한 뒤, 저장소 루트에서 다음 명령을 실행합니다. 시크릿 입력 프롬프트에 box에 저장한 것과 같은 키를 입력하세요.

```sh
cd worker
npx wrangler deploy
npx wrangler secret put INGEST_KEY
```

`server.py`의 `PAGE`를 수정하면 저장소 루트에서 `python3 worker/scripts/sync-html.py`를 실행해 Worker가 제공하는 `worker/src/html.js`를 다시 생성하세요.

### box에서 스냅샷 푸시

로컬 서버가 실행 중일 때 저장소 루트에서 원하는 방식으로 실행합니다.

```sh
./deploy/push-to-worker.py             # 한 번 확인
./deploy/push-to-worker.py --loop      # 15초마다 확인
./deploy/push-to-worker.py --loop 30   # 30초마다 확인
./deploy/push-to-worker.py --force     # 현재 스냅샷을 즉시 재전송
```

| 환경변수 | 기본값 | 용도 |
| --- | --- | --- |
| `SOURCE_URL` | `http://127.0.0.1:8787/api/tasks` | box의 스냅샷 조회 주소 |
| `WORKER_URL` | `https://task-dashboard.velar-agent.workers.dev` | Worker 기본 주소. 스크립트가 `/api/ingest`를 붙입니다. |
| `KEY_FILE` | `~/.config/task-dashboard/ingest-key` | `INGEST_KEY`와 같은 Bearer 키를 담은 파일 |

스크립트는 `tasks`만 해시하므로 `generated_at`만 달라지면 전송을 건너뜁니다. 전송에 성공하면 SHA-256 해시를 `/tmp/task-push-$UID.sha`에 기록합니다. 실패하면 표준 오류에 한 줄을 쓰고, 반복 모드에서는 다음 주기에 다시 확인합니다. `--loop`와 `--force`를 함께 쓰면 매 주기 전송합니다.

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
