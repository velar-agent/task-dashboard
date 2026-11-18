# Engineering 작업 현황

공용 JSONL 로그에서 Engineering 봇의 작업을 읽어 최근 업데이트 순으로 보여주는 한국어 대시보드입니다. Python 3 표준 라이브러리로 동작하며 화면에 필요한 HTML, CSS, JavaScript는 `server.py`에 들어 있습니다.

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

화면은 30초마다 갱신됩니다. 상태 탭으로 작업을 걸러 보고, 행이나 작업 제목을 누르면 이벤트 이력을 펼칠 수 있습니다. 표의 시각은 모두 한국 시각(KST)입니다.

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

화면의 목록과 탭 개수는 최신 상태의 `bot`이 `Engineering`인 작업을 기준으로 합니다. 이벤트 필드와 헬퍼 옵션은 [공용 로그 사용법](/workspace/task-status/README.md)에서 확인할 수 있습니다.

## 테스트

```sh
cd /workspace/task-dashboard
python3 -m unittest
```

테스트는 임시 로그와 메모리 안의 HTTP 요청을 사용합니다.

## Google VM 배포 (`deploy/`)
usage-dashboard 의 VM 방식과 같아요: GCE VM(`ssh gce-collector`)의 `/workspace/task-dashboard` 는 GitHub(`velar-agent/task-dashboard`, 비공개 — VM 의 읽기 전용 deploy key 로 pull) 체크아웃이고, systemd 유닛 `task-dashboard.service`(User=collector, `127.0.0.1:8788`)와 cloudflared quick tunnel 유닛 `cloudflared-task-dashboard.service` 로 노출해요.

- 재배포(box 에서, 먼저 `git push`): `deploy/deploy.sh` (VM 에서 `git pull --ff-only` → `deploy/install.sh` 로 유닛 설치·재시작)
- 상태 파일은 repo 에 없어요(`.gitignore`). box 의 `/workspace/task-status/tasks.jsonl` 이 원본이고, `deploy/sync-to-vm.sh --loop 15` 가 box 에서 15초마다 바뀐 때만 VM 의 같은 경로로 복사해요(VM 에서 `mv` 로 원자적 교체).
- 터널 URL: `ssh gce-collector 'journalctl -u cloudflared-task-dashboard -o cat | grep -o "https://[a-z0-9-]*\.trycloudflare.com" | tail -1'` (quick tunnel 이라 재시작하면 URL 이 바뀌어요)
- 로그: `journalctl -u task-dashboard`
