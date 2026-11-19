import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import quote

import server


EARLY = "2026-11-18T16:21:00+09:00"
LATE = "2026-11-18T16:30:00+09:00"


def event(task_id, ts, stage, **fields):
    return {"task_id": task_id, "ts": ts, "stage": stage, **fields}


class DashboardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "tasks.jsonl"

    def write_events(self, *records):
        self.path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                             encoding="utf-8")

    def get(self, target):
        # 실제 소켓이나 리스너 없이 http.server의 요청 파서와 응답 경로를 거친다.
        request = Mock()
        request.makefile.return_value = io.BytesIO(f"GET {target} HTTP/1.0\r\n\r\n".encode("ascii"))
        response = io.BytesIO()
        request.sendall.side_effect = response.write
        with patch.dict("os.environ", {"TASKS_FILE": str(self.path)}), patch.object(server.DashboardHandler, "log_message"):
            server.DashboardHandler(request, ("127.0.0.1", 0), None)
        headers, body = response.getvalue().split(b"\r\n\r\n", 1)
        status = int(headers.split(None, 2)[1])
        return status, headers, body

    def test_fold_by_time_with_stable_ties_and_independent_inheritance(self):
        self.write_events(
            event("a", "2026-11-18T07:30:00Z", "리뷰", title="바뀐 제목", note="검토", next="결과 공유"),
            event("a", EARLY, "계획", bot="초기 봇", title="첫 제목"),
            event("a", LATE, "완료", title="", bot="", note=""),
            event("a", "2026-11-18T16:25:00+09:00", "코딩", bot="Engineering"),
        )
        task, = server.load_tasks(self.path)
        self.assertEqual((task["started"], task["updated"]), (EARLY, LATE))
        self.assertEqual((task["bot"], task["title"]), ("Engineering", "바뀐 제목"))
        self.assertEqual((task["stage"], task["status"], task["note"], task["next"]), ("완료", "완료", "", ""))
        self.assertEqual([e["stage"] for e in task["events"]], ["계획", "코딩", "리뷰", "완료"])
        self.assertEqual([e["ts"] for e in task["events"][-2:]], [LATE, LATE])
        self.assertEqual(task["events"][-2]["next"], "결과 공유")
        self.assertEqual(task["events"][-1]["title"], "")

    def test_status_for_each_stage_and_latest_update_order(self):
        stages = ["접수", "계획", "코딩", "리뷰", "커밋", "배포", "점검", "완료", "막힘"]
        records = [event(stage, EARLY, stage) for stage in stages]
        records += [
            event("코딩", "2026-11-18T07:30:00.1Z", "코딩"),
            event("코딩", LATE, "막힘"),
            event("계획", LATE, "계획"),
        ]
        self.write_events(*records)
        tasks = server.load_tasks(self.path)
        self.assertEqual(len(tasks), len(stages))
        self.assertEqual([task["task_id"] for task in tasks[:2]], ["코딩", "계획"])
        self.assertEqual(tasks[0]["updated"], "2026-11-18T16:30:00.100000+09:00")
        statuses = {task["task_id"]: task["status"] for task in tasks}
        self.assertEqual(statuses, {s: s if s in ("완료", "막힘") else "진행 중" for s in stages})

    def test_completed_always_last_with_updated_desc_within_groups(self):
        # 완료는 업데이트가 가장 최근이어도 항상 뒤. 막힘은 앞 그룹에 남는다.
        self.write_events(
            event("done-old", "2026-11-18T08:00:00+09:00", "완료"),
            event("active-old", "2026-11-18T09:00:00+09:00", "코딩"),
            event("blocked", "2026-11-18T14:00:00+09:00", "막힘"),
            event("active-new", "2026-11-18T15:00:00+09:00", "리뷰"),
            event("done-new", LATE, "완료"),
        )
        tasks = server.load_tasks(self.path)
        self.assertEqual(
            [task["task_id"] for task in tasks],
            ["active-new", "blocked", "active-old", "done-new", "done-old"],
        )
        self.assertEqual(
            [(task["task_id"], task["stage"], task["status"]) for task in tasks],
            [
                ("active-new", "리뷰", "진행 중"),
                ("blocked", "막힘", "막힘"),
                ("active-old", "코딩", "진행 중"),
                ("done-new", "완료", "완료"),
                ("done-old", "완료", "완료"),
            ],
        )

    def test_intake_then_plan_folds_into_one_in_progress_task(self):
        self.write_events(
            event("a", EARLY, "접수", bot="Engineering", title="막 받은 요청"),
            event("a", LATE, "계획", note="계획 수립", next="코드 작성"),
        )
        task, = server.load_tasks(self.path)
        self.assertEqual(task["task_id"], "a")
        self.assertEqual([e["stage"] for e in task["events"]], ["접수", "계획"])
        self.assertEqual((task["stage"], task["status"]), ("계획", "진행 중"))
        self.assertEqual((task["started"], task["updated"]), (EARLY, LATE))
        self.assertEqual((task["bot"], task["title"]), ("Engineering", "막 받은 요청"))

    def test_invalid_records_do_not_change_state_or_hide_later_lines(self):
        self.write_events(
            event("a", EARLY, "계획", title="보존할 제목"),
            None, [], 42,
            event("a", LATE, "다른 단계", title="잘못된 제목"),
            event("a", LATE, {"bad": "stage"}),
            event([], LATE, "막힘"),
            event(" ", LATE, "완료"),
            event("a", "잘못된 시각", "완료"),
            event("a", "2026-11-18T16:30:00", "완료"),
            {"task_id": "a", "stage": "코딩"},
        )
        with self.path.open("ab") as stream:
            stream.write(b'\xff\n{"broken":\n\n')
            stream.write(json.dumps(event("a", LATE, "점검", note="정상 줄")).encode() + b"\n")
        with self.assertLogs(server.LOGGER, "WARNING"):
            task, = server.load_tasks(self.path)
        self.assertEqual([e["stage"] for e in task["events"]], ["계획", "점검"])
        self.assertEqual((task["title"], task["note"]), ("보존할 제목", "정상 줄"))

    def test_partial_last_line_appears_once_completed(self):
        self.write_events(event("a", EARLY, "계획"))
        line = json.dumps(event("a", LATE, "코딩"), ensure_ascii=False).encode("utf-8")
        with self.path.open("ab") as stream:
            stream.write(line[:-2])
        with self.assertLogs(server.LOGGER, "WARNING"):
            self.assertEqual(server.load_tasks(self.path)[0]["stage"], "계획")
        with self.path.open("ab") as stream:
            stream.write(line[-2:] + b"\n")
        self.assertEqual(server.load_tasks(self.path)[0]["stage"], "코딩")

    def test_missing_and_empty_logs(self):
        self.assertEqual(server.load_tasks(self.path), [])
        self.assertFalse(self.path.exists())
        self.path.touch()
        self.assertEqual(server.load_tasks(self.path), [])

    def test_list_and_detail_api(self):
        task_id = "릴리스/1 ?+#"
        self.write_events(
            event(task_id, EARLY, "계획", bot="Engineering", title="<em>제목</em>"),
            event(task_id, LATE, "코딩", next="리뷰 준비"),
        )
        status, headers, body = self.get("/api/tasks")
        self.assertEqual(status, 200)
        self.assertIn(b"application/json; charset=utf-8", headers)
        payload = json.loads(body)
        self.assertEqual(datetime.fromisoformat(payload["generated_at"]).utcoffset(), timedelta(hours=9))
        task, = payload["tasks"]
        self.assertEqual(set(task), {"task_id", "bot", "title", "stage", "status", "started", "updated", "note", "next", "events"})
        status, _, detail = self.get("/api/tasks/" + quote(task_id, safe="") + "?view=detail")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(detail), task)
        self.assertEqual(len(task["events"]), 2)
        self.assertEqual(self.get("/api/tasks/missing")[0], 404)

    def test_page_health_and_read_errors(self):
        self.path.mkdir()
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"text/html; charset=utf-8", headers)
        self.assertIn("작업 현황", body.decode("utf-8"))
        status, _, body = self.get("/healthz")
        self.assertEqual((status, json.loads(body)), (200, {"status": "ok"}))
        self.assertEqual(self.get("/missing")[0], 404)
        with self.assertLogs(server.LOGGER, "ERROR"):
            self.assertEqual(self.get("/api/tasks")[0], 503)


if __name__ == "__main__":
    unittest.main()
