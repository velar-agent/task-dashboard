import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import server


CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


@unittest.skipUnless(CHROME, "화면 테스트에는 Chrome 또는 Chromium이 필요합니다.")
class DashboardUITests(unittest.TestCase):
    def run_page(self, page, width):
        log = page.with_name("chrome.log")
        with log.open("wb") as stream:
            browser = subprocess.Popen([
                CHROME, "--headless", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                f"--window-size={width},800",
                "--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--no-first-run", "--no-proxy-server", "--host-resolver-rules=MAP * ~NOTFOUND",
                "--enable-logging=stderr", f"--user-data-dir={page.parent / 'profile'}", page.as_uri(),
            ], stdout=subprocess.DEVNULL, stderr=stream)
            try:
                # --dump-dom은 정적 페이지에서도 종료하지 않을 수 있다. 마지막 DOM 관찰을 기다린다.
                deadline = time.monotonic() + 10
                while True:
                    messages = log.read_text(encoding="utf-8", errors="replace")
                    result = re.search(r"PAGE_RESULT (\{.*\})", messages)
                    if result:
                        return json.loads(result[1])
                    if browser.poll() is not None or time.monotonic() >= deadline:
                        self.fail("Chrome에서 화면 검사 결과를 받지 못했습니다.\n" + messages[-3000:])
                    time.sleep(0.05)
            finally:
                browser.terminate()
                try:
                    browser.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    browser.kill()
                    browser.wait(timeout=3)

    def observe_page(self, exercise, width=1280):
        # 파일로 연 실제 PAGE에 스냅샷과 30초 타이머만 주입한다.
        setup = """
        <script>
        const stamp = "2026-11-18T12:00:00+09:00";
        function task(task_id, bot, status) {
          return {task_id, bot, status, stage: status, started: stamp, updated: stamp, events: []};
        }
        const active = {...task("active", "Engineering", "진행 중"), title: "기능 구현", stage: "코딩",
                        started: "2026-11-18T10:00:00+09:00", note: "구현 중", next: "리뷰 요청"};
        const done = task("done", "Engineering", "완료");
        const blocked = task("blocked", "<b>QA</b>", "막힘");
        const unnamed = task("unnamed", "", "완료");
        let snapshot = {tasks: [active, blocked, unnamed, done], generated_at: stamp};
        window.fetch = async path => ({ok: path === "/api/tasks", json: async () => snapshot});
        let tick;
        window.setInterval = (callback, ms) => { if (ms === 30000) tick = callback; };
        </script>
        """
        probe = """
        <script>
        window.addEventListener("load", async () => {
          const observations = {};
          try {
        """ + exercise + """
          } catch (error) {
            observations.error = String(error);
          }
          console.info("PAGE_RESULT " + JSON.stringify(observations));
        });
        </script>
        """
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / "test.html"
            page.write_text(server.PAGE.replace("</head>", setup + "</head>")
                            .replace("</body>", probe + "</body>"), encoding="utf-8")
            data = self.run_page(page, width)
        self.assertNotIn("error", data)
        return data

    def test_columns_and_title_toggle(self):
        exercise = """
            const table = document.querySelector(".tasks");
            const headers = [...table.tHead.rows[0].cells];
            const columns = table.querySelector("colgroup").children;
            const cells = table.tBodies[0].rows[0].cells;
            observations.columns = headers.map((th, i) => [th.textContent, columns[i].className, cells[i].textContent]);
            observations.aligned = headers.every((th, i) => th.scope === "col" && th.offsetLeft === cells[i].offsetLeft
              && (i === 0 || headers[i - 1].offsetLeft < th.offsetLeft));
            observations.sortedBy = table.querySelector('th[aria-sort="descending"]').textContent;
            observations.mobile = matchMedia("(max-width: 700px)").matches;

            const toggle = cells[2].querySelector("button.task-toggle");
            toggle.focus();
            toggle.click();
            await tick();
            const history = table.querySelector(".task-row + .history");
            const focused = document.activeElement;
            observations.open = [focused.dataset.taskId, focused.getAttribute("aria-expanded"),
                                 focused.getAttribute("aria-controls") === history.id, history.cells[0].colSpan];
            focused.click();
            observations.closed = [document.activeElement.dataset.taskId, document.activeElement.getAttribute("aria-expanded"),
                                   table.querySelector(".history") === null];
        """
        for width in (1280, 390):
            with self.subTest(width=width):
                data = self.observe_page(exercise, width)
                self.assertEqual(data["mobile"], width == 390)
                self.assertTrue(data["aligned"])
                self.assertEqual(data["sortedBy"], "마지막 업데이트")
                self.assertEqual(data["columns"], [
                    ["봇", "col-bot", "Engineering"], ["현재 단계", "col-stage", "코딩"],
                    ["작업", "col-title", "기능 구현active"], ["시작", "col-time", "2026-11-18 10:00"],
                    ["마지막 업데이트", "col-time", "2026-11-18 12:00"],
                    ["메모", "col-note", "구현 중"], ["다음 할 일", "col-next", "리뷰 요청"],
                ])
                self.assertEqual(data["open"], ["active", "true", True, 7])
                self.assertEqual(data["closed"], ["active", "false", True])

    def test_filters_counts_and_selection_across_refreshes(self):
        data = self.observe_page("""
            const select = document.getElementById("bot-filter");
            const record = () => [
              select.value,
              [...document.querySelectorAll(".count")].map(node => Number(node.textContent)),
              [...document.querySelectorAll(".task-toggle")].map(node => node.dataset.taskId)
            ];
            function choose(name) {
              select.value = name;
              select.dispatchEvent(new Event("change"));
            }
            observations.options = [...select.options].map(option => option.textContent);
            observations.markupNodes = document.querySelectorAll("#bot-filter b, #rows b").length;
            observations.all = record();
            choose("<b>QA</b>");
            observations.qa = record();
            choose("");
            observations.backToAll = record();
            choose("Engineering");
            observations.engineering = record();
            document.querySelector('[data-status="완료"]').click();
            observations.done = record();
            snapshot.tasks = [done, unnamed, task("deploy", "Engineering", "막힘")];
            await tick();
            observations.refresh = record();
            snapshot.tasks = [blocked, {...unnamed, bot: "  "}];
            await tick();
            observations.fallback = record();
            observations.fallbackOptions = [...select.options].map(option => option.textContent);
            choose("(미지정)");
            observations.unnamed = record();
            observations.unnamedCell = document.querySelector(".task-row").cells[0].textContent;
            snapshot = {tasks: [], generated_at: null};
            await tick();
            observations.empty = record();
            observations.emptyLoaded = document.getElementById("notice").hidden;
        """)
        self.assertEqual(data["options"][0], "전체")
        self.assertCountEqual(data["options"][1:], ["(미지정)", "<b>QA</b>", "Engineering"])
        self.assertEqual(data["markupNodes"], 0)
        self.assertEqual(data["all"], ["", [4, 1, 2, 1], ["active", "blocked", "unnamed", "done"]])
        self.assertEqual(data["qa"], ["<b>QA</b>", [1, 0, 0, 1], ["blocked"]])
        self.assertEqual(data["backToAll"], data["all"])
        self.assertEqual(data["engineering"], ["Engineering", [2, 1, 1, 0], ["active", "done"]])
        self.assertEqual(data["done"], ["Engineering", [2, 1, 1, 0], ["done"]])
        self.assertEqual(data["refresh"], ["Engineering", [2, 0, 1, 1], ["done"]])
        self.assertEqual(data["fallback"], ["", [2, 0, 1, 1], ["unnamed"]])
        self.assertCountEqual(data["fallbackOptions"], ["전체", "(미지정)", "<b>QA</b>"])
        self.assertEqual(data["unnamed"], ["(미지정)", [1, 0, 1, 0], ["unnamed"]])
        self.assertEqual(data["unnamedCell"], "(미지정)")
        self.assertEqual(data["empty"], ["", [0, 0, 0, 0], []])
        self.assertTrue(data["emptyLoaded"])
