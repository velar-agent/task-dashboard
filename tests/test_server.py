import io
import json
import os
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
        # 바깥 환경의 BOT_ALIASES_FILE이 기본 경로 테스트에 섞이지 않게 한다.
        env_patcher = patch.dict(os.environ)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("BOT_ALIASES_FILE", None)
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "tasks.jsonl"

    def write_events(self, *records):
        self.path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
                             encoding="utf-8")

    def alias_path(self):
        return self.path.parent / "bot-aliases.json"

    def write_aliases(self, mapping):
        self.alias_path().write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

    def write_aliases_raw(self, text):
        self.alias_path().write_text(text, encoding="utf-8")

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
        # 기본 순서는 상태 그룹(진행 중 → 막힘 → 완료)이며, 그룹 안은
        # task_id 자연 정렬이다. 마지막 업데이트는 기본 순서가 아니다.
        ranks = {"진행 중": 0, "막힘": 1, "완료": 2}
        self.assertEqual([ranks[task["status"]] for task in tasks],
                         sorted(ranks[task["status"]] for task in tasks))
        self.assertEqual([task["task_id"] for task in tasks[-2:]], ["막힘", "완료"])
        progress_ids = [task["task_id"] for task in tasks if task["status"] == "진행 중"]
        self.assertEqual(progress_ids, sorted(progress_ids, key=server._natural_task_id_key))
        by_id = {task["task_id"]: task for task in tasks}
        self.assertEqual(by_id["코딩"]["updated"], "2026-11-18T16:30:00.100000+09:00")
        statuses = {task["task_id"]: task["status"] for task in tasks}
        self.assertEqual(statuses, {s: s if s in ("완료", "막힘") else "진행 중" for s in stages})

    def test_completed_always_last_with_task_id_order_within_groups(self):
        # 완료는 업데이트가 가장 최근이어도 항상 뒤. 막힘은 완료 앞이다.
        # 같은 상태 그룹 안에서는 task_id 자연 정렬이다(업데이트 순이 아니다).
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
            ["active-new", "active-old", "blocked", "done-new", "done-old"],
        )
        self.assertEqual(
            [(task["task_id"], task["stage"], task["status"]) for task in tasks],
            [
                ("active-new", "리뷰", "진행 중"),
                ("active-old", "코딩", "진행 중"),
                ("blocked", "막힘", "막힘"),
                ("done-new", "완료", "완료"),
                ("done-old", "완료", "완료"),
            ],
        )

    def test_status_groups_natural_id_order_regardless_of_updated(self):
        # 상태 그룹(진행 중 → 막힘 → 완료) 안에서는 task_id 자연 정렬이
        # 업데이트보다 우선한다. task-2가 task-10보다 앞에 오고,
        # 대소문자는 구분하지 않는다. 숫자 시작 ID는 JS 로케일 비교처럼
        # 문자 시작 ID보다 앞에 온다.
        self.write_events(
            event("task-10", "2026-11-18T08:00:00+09:00", "코딩"),
            event("task-2", "2026-11-18T15:00:00+09:00", "코딩"),
            event("Task-1", "2026-11-18T12:00:00+09:00", "코딩"),
            event("a", "2026-11-18T16:00:00+09:00", "코딩"),
            event("B", "2026-11-18T07:00:00+09:00", "코딩"),
            event("10", "2026-11-18T16:00:00+09:00", "코딩"),
            event("1", "2026-11-18T07:00:00+09:00", "코딩"),
            event("blocked-z", "2026-11-18T16:30:00+09:00", "막힘"),
            event("blocked-a", "2026-11-18T07:30:00+09:00", "막힘"),
            event("done-10", "2026-11-18T16:29:00+09:00", "완료"),
            event("done-2", "2026-11-18T16:28:00+09:00", "완료"),
        )
        tasks = server.load_tasks(self.path)
        self.assertEqual(
            [task["task_id"] for task in tasks],
            ["1", "10", "a", "B", "Task-1", "task-2", "task-10",
             "blocked-a", "blocked-z", "done-2", "done-10"],
        )
        self.assertEqual(
            [task["status"] for task in tasks],
            ["진행 중"] * 7 + ["막힘"] * 2 + ["완료"] * 2,
        )

    def test_task_status_derives_from_stage_when_status_missing(self):
        # status가 없거나 비어 있으면 stage에서 유도하고, 있으면 status가
        # stage보다 우선한다. 접수부터 점검까지는 모두 진행 중이다.
        for stage in ("접수", "계획", "코딩", "리뷰", "커밋", "배포", "점검"):
            self.assertEqual(server._task_status({"stage": stage}), "진행 중")
        self.assertEqual(server._task_status({"stage": "완료"}), "완료")
        self.assertEqual(server._task_status({"stage": "막힘"}), "막힘")
        self.assertEqual(server._task_status({}), "진행 중")
        self.assertEqual(server._task_status({"status": "", "stage": "완료"}), "완료")
        self.assertEqual(server._task_status({"status": "막힘", "stage": "완료"}), "막힘")
        self.assertEqual(server._task_status({"status": "진행 중", "stage": "완료"}), "진행 중")

    def test_natural_key_matches_js_locale_order_for_common_ids(self):
        # Python 근사 키가 JS localeCompare(numeric, base) 순서와 같은
        # 대표 ID 집합에 대해 같은 순서를 내는지 본다.
        ids = ["task-2", "task-10", "Task-1", "a", "A", "B", "a2", "a10", "1", "10", "2", ""]
        self.assertEqual(
            sorted(ids, key=server._natural_task_id_key),
            ["", "1", "2", "10", "a", "A", "a2", "a10", "B", "Task-1", "task-2", "task-10"],
        )

    def test_task_id_compare_tiebreak_by_updated_desc(self):
        # "Task-1"과 "task-1"은 기본 비교(base)에서 동률이다. 같은 상태
        # 그룹이면 마지막 업데이트가 최근인 작업이 먼저 온다. load_tasks는
        # task_id로 묶으므로 서로 다른 대소문자 ID로만 관찰할 수 있다.
        self.write_events(
            event("Task-1", EARLY, "코딩"),
            event("task-1", LATE, "코딩"),
            event("a-task", "2026-11-18T12:00:00+09:00", "코딩"),
        )
        tasks = server.load_tasks(self.path)
        self.assertEqual([task["task_id"] for task in tasks], ["a-task", "task-1", "Task-1"])

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
        self.assertEqual(set(task), {"task_id", "bot_id", "bot_name", "bot", "title", "stage", "status", "started", "updated", "note", "next", "events"})
        status, _, detail = self.get("/api/tasks/" + quote(task_id, safe="") + "?view=detail")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(detail), task)
        self.assertEqual(len(task["events"]), 2)
        self.assertEqual(self.get("/api/tasks/missing")[0], 404)

    def test_read_event_parses_bot_id_and_names(self):
        line = json.dumps(event("a", EARLY, "계획", bot_id="bot-1", bot_name="NewBot", bot="OldBot", title="T"),
                          ensure_ascii=False).encode("utf-8")
        parsed = server.read_event(line)
        self.assertEqual(parsed["bot_id"], "bot-1")
        self.assertEqual(parsed["bot_name"], "NewBot")
        self.assertEqual(parsed["bot"], "NewBot")
        self.assertEqual(parsed["recorded_bot_name"], "NewBot")
        legacy = json.dumps(event("a", EARLY, "계획", bot="OldBot", title="T"), ensure_ascii=False).encode("utf-8")
        parsed = server.read_event(legacy)
        self.assertEqual((parsed["bot_id"], parsed["recorded_bot_name"]), ("", "OldBot"))
        parsed = server.read_event(legacy, {"OldBot": "bot-1"})
        self.assertEqual((parsed["bot_id"], parsed["recorded_bot_name"]), ("bot-1", "OldBot"))
        for bad_id in ("bad id", " bot-1", "bot-1 ", "", 123, None, True):
            record = json.dumps(event("a", EARLY, "계획", bot_id=bad_id, bot_name="Named", title="T"),
                                ensure_ascii=False).encode("utf-8")
            self.assertEqual(server.read_event(record)["bot_id"], "", repr(bad_id))
        # bot_name 우선, 비문자열 bot_name은 bot으로 폴백한다.
        record = json.dumps(event("a", EARLY, "계획", bot_name=123, bot="Fallback", title="T"),
                            ensure_ascii=False).encode("utf-8")
        self.assertEqual(server.read_event(record)["recorded_bot_name"], "Fallback")

    def test_alias_groups_legacy_lines_and_renamed_current_name(self):
        self.write_aliases({"OldBot": "bot-1"})
        self.write_events(
            event("a", EARLY, "계획", bot="OldBot", title="옛 작업"),
            event("b", LATE, "코딩", bot_id="bot-1", bot_name="NewBot", bot="NewBot", title="새 작업"),
        )
        by_id = {task["task_id"]: task for task in server.load_tasks(self.path)}
        for task in by_id.values():
            self.assertEqual((task["bot_id"], task["bot_name"], task["bot"]), ("bot-1", "NewBot", "NewBot"))
        past = by_id["a"]["events"][0]
        self.assertEqual((past["recorded_bot_name"], past["bot_name"], past["bot"], past["bot_id"]),
                         ("OldBot", "NewBot", "NewBot", "bot-1"))

    def test_explicit_name_wins_over_later_stale_alias_line(self):
        self.write_aliases({"OldBot": "bot-1"})
        self.write_events(
            event("a", "2026-11-18T09:00:00+09:00", "계획", bot_id="bot-1", bot_name="NewBot", bot="NewBot", title="T"),
            event("b", "2026-11-18T12:00:00+09:00", "계획", bot="OldBot", title="stale"),
        )
        tasks = server.load_tasks(self.path)
        for task in tasks:
            self.assertEqual((task["bot_id"], task["bot_name"], task["bot"]), ("bot-1", "NewBot", "NewBot"))
            for item in task["events"]:
                self.assertEqual(item["bot_name"], "NewBot")
        stale = {task["task_id"]: task for task in tasks}["b"]["events"][0]
        self.assertEqual(stale["recorded_bot_name"], "OldBot")

    def test_inferred_latest_name_when_no_explicit_and_tie_break(self):
        self.write_aliases({"Old1": "bot-9", "Old2": "bot-9"})
        self.write_events(
            event("a", EARLY, "계획", bot="Old1", title="T"),
            event("a", LATE, "코딩", bot="Old2"),
        )
        task, = server.load_tasks(self.path)
        self.assertEqual((task["bot_id"], task["bot_name"]), ("bot-9", "Old2"))
        self.assertEqual([item["bot_name"] for item in task["events"]], ["Old2", "Old2"])
        self.write_events(
            event("a", EARLY, "계획", bot_id="bot-2", bot_name="First", title="T"),
            event("a", EARLY, "코딩", bot_id="bot-2", bot_name="Second"),
        )
        task, = server.load_tasks(self.path)
        self.assertEqual(task["bot_name"], "Second")

    def test_aliasless_idless_name_kept_as_is(self):
        self.write_aliases({})
        self.write_events(
            event("a", EARLY, "계획", bot="Solo", title="T"),
            event("a", LATE, "코딩"),
        )
        task, = server.load_tasks(self.path)
        self.assertEqual((task["bot_id"], task["bot_name"], task["bot"]), ("", "Solo", "Solo"))
        self.assertEqual([item["recorded_bot_name"] for item in task["events"]], ["Solo", ""])

    def test_invalid_alias_files_warn_and_fall_back_to_empty(self):
        self.write_events(event("a", EARLY, "계획", bot="OldBot", title="T"))
        with self.assertLogs(server.LOGGER, "WARNING") as logs:
            task, = server.load_tasks(self.path)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual((task["bot_id"], task["bot_name"]), ("", "OldBot"))
        self.write_aliases_raw("{ not json")
        with self.assertLogs(server.LOGGER, "WARNING") as logs:
            task, = server.load_tasks(self.path)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual((task["bot_id"], task["bot_name"]), ("", "OldBot"))
        self.write_aliases_raw('["not", "a", "dict"]')
        with self.assertLogs(server.LOGGER, "WARNING") as logs:
            task, = server.load_tasks(self.path)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual((task["bot_id"], task["bot_name"]), ("", "OldBot"))
        self.write_aliases({"Good": "bot-ok", "BadEmpty": "", "BadSpace": "has space", "BadType": 123})
        with self.assertLogs(server.LOGGER, "WARNING"):
            tasks = server.load_tasks(self.path)
        self.assertEqual(tasks[0]["bot_name"], "OldBot")
        self.write_events(
            event("a", EARLY, "계획", bot="Good", title="T"),
            event("b", EARLY, "계획", bot="BadSpace", title="T2"),
        )
        with self.assertLogs(server.LOGGER, "WARNING"):
            by_id = {task["task_id"]: task for task in server.load_tasks(self.path)}
        self.assertEqual((by_id["a"]["bot_id"], by_id["a"]["bot_name"]), ("bot-ok", "Good"))
        self.assertEqual((by_id["b"]["bot_id"], by_id["b"]["bot_name"]), ("", "BadSpace"))

    def test_aliases_env_and_explicit_path(self):
        custom = self.path.parent / "custom-aliases.json"
        custom.write_text(json.dumps({"Legacy": "bot-env"}, ensure_ascii=False), encoding="utf-8")
        self.write_events(event("a", EARLY, "계획", bot="Legacy", title="T"))
        with patch.dict("os.environ", {"BOT_ALIASES_FILE": str(custom)}):
            task, = server.load_tasks(self.path)
        self.assertEqual((task["bot_id"], task["bot_name"]), ("bot-env", "Legacy"))
        with patch.dict("os.environ", {"BOT_ALIASES_FILE": str(custom)}):
            with self.assertLogs(server.LOGGER, "WARNING"):
                task, = server.load_tasks(self.path, aliases_path=str(self.alias_path()))
        self.assertEqual((task["bot_id"], task["bot_name"]), ("", "Legacy"))

    def test_task_bot_inheritance_uses_current_name_and_id(self):
        self.write_aliases({})
        self.write_events(
            event("a", EARLY, "계획", bot_id="b1", bot_name="Alpha", title="T"),
            event("a", LATE, "코딩"),
            event("b", EARLY, "계획", bot_id="b1", bot_name="Alpha", title="T2"),
            event("b", LATE, "코딩", bot_id="b2", bot_name="Beta"),
        )
        by_id = {task["task_id"]: task for task in server.load_tasks(self.path)}
        self.assertEqual((by_id["a"]["bot_id"], by_id["a"]["bot_name"], by_id["a"]["bot"]), ("b1", "Alpha", "Alpha"))
        self.assertEqual((by_id["b"]["bot_id"], by_id["b"]["bot_name"], by_id["b"]["bot"]), ("b2", "Beta", "Beta"))

    def test_detail_api_includes_bot_identity_fields(self):
        self.write_aliases({"OldBot": "bot-1"})
        self.write_events(
            event("renamed", EARLY, "계획", bot="OldBot", title="옛 이름 작업"),
            event("renamed", LATE, "코딩", bot_id="bot-1", bot_name="NewBot", bot="NewBot"),
        )
        status, _, body = self.get("/api/tasks/renamed")
        self.assertEqual(status, 200)
        detail = json.loads(body)
        self.assertEqual((detail["bot_id"], detail["bot_name"], detail["bot"]), ("bot-1", "NewBot", "NewBot"))
        self.assertEqual(detail["events"][0]["recorded_bot_name"], "OldBot")
        self.assertEqual(detail["events"][0]["bot_name"], "NewBot")
        status, _, listed = self.get("/api/tasks")
        self.assertEqual(json.loads(listed)["tasks"][0], detail)

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
