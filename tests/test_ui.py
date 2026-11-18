import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import server


CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")


def _profile_users(profile_dir):
    """--user-data-dir=<profile_dir>를 쓰는 살아있는 프로세스 pid 목록."""
    needle = str(profile_dir).encode()
    users = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join("/proc", entry, "cmdline"), "rb") as stream:
                cmdline = stream.read()
            if needle not in cmdline:
                continue
            with open(os.path.join("/proc", entry, "stat"), encoding="utf-8", errors="replace") as stream:
                state = stream.read().rsplit(")", 1)[-1].split()[0]
            if state == "Z":
                continue
        except OSError:
            continue
        users.append(int(entry))
    return users


def _kill_browser(browser, profile_dir):
    """Chrome 메인 프로세스를 우아하게 종료한 뒤 남은 자식 프로세까지 끝낸다.

    메인 프로세스에 SIGTERM을 보내 종료를 기다린 뒤, 메인 종료 후에도
    프로필을 쓰는 프로세스가 남으면 그 그룹을 강제 종료해 정리로 넘어가도
    디렉터리가 비지 않는 레이스가 없게 한다.
    """
    browser.terminate()
    try:
        browser.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(browser.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            browser.kill()
        browser.wait(timeout=3)
    # Chrome 자식이 메인 프로세스보다 늦게 사는 경우가 있다. 프로필을
    # 여전히 쓰는 프로세스가 있으면 그룹 단위로 강제 종료하고 프로필이
    # 해제될 때까지 기다린다.
    deadline = time.monotonic() + 5
    while True:
        users = _profile_users(profile_dir)
        if not users:
            return
        for pid in users:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def _remove_tree(path):
    """Chrome이 프로필 디렉터리를 아직 쓰다 놓는 경우를 위해 재시도 후 무시한다."""
    for attempt in range(8):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt == 7:
                break
            time.sleep(0.2 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def _temporary_directory():
    directory = tempfile.mkdtemp()
    try:
        yield directory
    finally:
        _remove_tree(directory)


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
            ], stdout=subprocess.DEVNULL, stderr=stream, start_new_session=True)
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
                _kill_browser(browser, page.parent / "profile")

    def _page_source(self, exercise, extra_setup=""):
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
        """ + extra_setup + """
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
        return server.PAGE.replace("</head>", setup + "</head>").replace("</body>", probe + "</body>")

    def observe_page(self, exercise, width=1280, extra_setup=""):
        with _temporary_directory() as directory:
            page = Path(directory) / "test.html"
            page.write_text(self._page_source(exercise, extra_setup), encoding="utf-8")
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

            const titleToggle = cells[2].querySelector("button.task-toggle");
            const taskIdNode = titleToggle.querySelector(".task-id");
            const taskTitleNode = titleToggle.querySelector(".task-title");
            observations.taskOrder = [taskIdNode.textContent, taskTitleNode.textContent,
              [...titleToggle.querySelector(".task-label").children].map(node => node.className),
              taskIdNode.getBoundingClientRect().top < taskTitleNode.getBoundingClientRect().top,
              taskIdNode.querySelectorAll("*").length, taskTitleNode.querySelectorAll("*").length];
            const taskIdComputed = getComputedStyle(taskIdNode);
            observations.taskIdStyle = [taskIdComputed.fontFamily, taskIdComputed.fontSize,
              taskIdComputed.color, taskIdComputed.backgroundColor, taskIdComputed.borderRadius,
              taskIdComputed.borderStyle];
            const unnamedToggle = [...document.querySelectorAll(".task-toggle")]
              .find(node => node.dataset.taskId === "unnamed");
            observations.unnamedTask = [unnamedToggle.textContent,
              unnamedToggle.querySelectorAll(".task-id").length,
              unnamedToggle.querySelectorAll(".task-title").length,
              unnamedToggle.querySelector(".task-id")?.textContent];

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
            snapshot.tasks = [...snapshot.tasks, {task_id: "<i>x</i>", bot: "Engineering", title: "<b>y</b>",
              stage: "코딩", status: "진행 중", started: stamp, updated: stamp, events: []}];
            await tick();
            const markupToggle = [...document.querySelectorAll(".task-toggle")]
              .find(node => node.dataset.taskId === "<i>x</i>");
            observations.markupTask = [markupToggle.querySelector(".task-id")?.textContent,
              markupToggle.querySelector(".task-title")?.textContent,
              markupToggle.querySelectorAll("b, i").length];
        """
        for width in (1280, 390):
            with self.subTest(width=width):
                data = self.observe_page(exercise, width)
                self.assertEqual(data["mobile"], width == 390)
                self.assertTrue(data["aligned"])
                self.assertEqual(data["sortedBy"], "마지막 업데이트")
                self.assertEqual(data["columns"], [
                    ["봇", "col-bot", "Engineering"], ["현재 단계", "col-stage", "코딩"],
                    ["작업", "col-title", "active기능 구현"], ["시작", "col-time", "2026-11-18 10:00"],
                    ["마지막 업데이트", "col-time", "2026-11-18 12:00"],
                    ["메모", "col-note", "구현 중"], ["다음 할 일", "col-next", "리뷰 요청"],
                ])
                self.assertEqual(data["taskOrder"], ["active", "기능 구현", ["task-id", "task-title"], True, 0, 0])
                font_family, font_size, color, background, radius, border = data["taskIdStyle"]
                self.assertIn("monospace", font_family.lower())
                self.assertGreaterEqual(float(font_size.removesuffix("px")), 13)
                self.assertEqual(color, "rgb(25, 79, 120)")
                self.assertEqual(background, "rgb(228, 239, 247)")
                self.assertEqual(radius, "999px")
                self.assertEqual(border, "solid")
                self.assertEqual(data["unnamedTask"], ["unnamed", 1, 0, "unnamed"])
                self.assertEqual(data["markupTask"], ["<i>x</i>", "<b>y</b>", 0])
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

    def test_column_selector_defaults_and_toggle(self):
        exercise = """
            const table = document.querySelector(".tasks");
            const button = document.getElementById("columns-button");
            const panel = document.getElementById("columns-panel");
            const headersOf = () => [...table.tHead.rows[0].cells].map(node => node.textContent);
            const colsOf = () => [...table.querySelector("colgroup").children].map(node => node.className);
            const firstCellsOf = () => [...table.tBodies[0].rows[0].cells].map(node => node.textContent);
            const rowCounts = () => [...table.querySelectorAll(".task-row")].map(row => row.cells.length);
            const inputsOf = () => [...panel.querySelectorAll('input[type="checkbox"]')];
            const byKey = key => panel.querySelector(`input[data-column="${key}"]`);
            observations.button = [button.textContent, button.getAttribute("aria-haspopup"),
              button.getAttribute("aria-expanded"), button.getAttribute("aria-controls"),
              button.getAttribute("type")];
            observations.panelHidden = panel.hidden;
            observations.labels = [...panel.querySelectorAll("label")].map(node => node.textContent);
            observations.labelMarks = panel.querySelectorAll("b, i").length;
            observations.initial = [headersOf(), colsOf(), firstCellsOf(), rowCounts(),
              inputsOf().map(node => [node.dataset.column, node.checked, node.disabled])];
            const activeToggle = () => [...document.querySelectorAll(".task-toggle")]
              .find(node => node.dataset.taskId === "active");
            activeToggle().click();
            observations.historyDefault = table.querySelector(".history")?.cells[0].colSpan;
            button.click();
            const rect = panel.getBoundingClientRect();
            observations.opened = [panel.hidden, button.getAttribute("aria-expanded"),
              panel.querySelectorAll("input").length];
            observations.noOverflow = [document.documentElement.scrollWidth <= window.innerWidth,
              document.body.scrollWidth <= window.innerWidth, rect.left >= -1,
              rect.right <= window.innerWidth + 1, window.innerWidth - rect.right >= -1];
            byKey("stage").click();
            byKey("note").click();
            observations.hidden = [headersOf(), colsOf(), firstCellsOf(), rowCounts(),
              table.querySelector(".history")?.cells[0].colSpan,
              inputsOf().map(node => [node.dataset.column, node.checked])];
            observations.stillOpen = [panel.hidden, button.getAttribute("aria-expanded")];
            byKey("note").click();
            observations.shown = [headersOf(), colsOf(), rowCounts(),
              table.querySelector(".history")?.cells[0].colSpan];
        """
        for width in (1280, 390):
            with self.subTest(width=width):
                data = self.observe_page(exercise, width)
                self.assertEqual(data["button"], ["열 ▾", "true", "false", "columns-panel", "button"])
                self.assertTrue(data["panelHidden"])
                self.assertEqual(data["labels"], ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
                self.assertEqual(data["labelMarks"], 0)
                self.assertEqual(data["initial"][0], ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
                self.assertEqual(data["initial"][1], ["col-bot", "col-stage", "col-title", "col-time", "col-time", "col-note", "col-next"])
                self.assertEqual(data["initial"][2], ["Engineering", "코딩", "active기능 구현", "2026-11-18 10:00", "2026-11-18 12:00", "구현 중", "리뷰 요청"])
                self.assertEqual(data["initial"][3], [7, 7, 7, 7])
                self.assertEqual([key for key, checked, disabled in data["initial"][4]],
                                 ["bot", "stage", "title", "started", "updated", "note", "next"])
                self.assertTrue(all(checked for _, checked, _ in data["initial"][4]))
                self.assertFalse(any(disabled for _, _, disabled in data["initial"][4]))
                self.assertEqual(data["historyDefault"], 7)
                self.assertEqual(data["opened"], [False, "true", 7])
                self.assertTrue(all(data["noOverflow"]))
                self.assertEqual(data["hidden"][0], ["봇", "작업", "시작", "마지막 업데이트", "다음 할 일"])
                self.assertEqual(data["hidden"][1], ["col-bot", "col-title", "col-time", "col-time", "col-next"])
                self.assertEqual(data["hidden"][2], ["Engineering", "active기능 구현", "2026-11-18 10:00", "2026-11-18 12:00", "리뷰 요청"])
                self.assertEqual(data["hidden"][3], [5, 5, 5, 5])
                self.assertEqual(data["hidden"][4], 5)
                self.assertEqual(dict(data["hidden"][5]),
                                 {"bot": True, "stage": False, "title": True, "started": True, "updated": True, "note": False, "next": True})
                self.assertEqual(data["stillOpen"], [False, "true"])
                self.assertEqual(data["shown"][0], ["봇", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
                self.assertEqual(data["shown"][1], ["col-bot", "col-title", "col-time", "col-time", "col-note", "col-next"])
                self.assertEqual(data["shown"][2], [6, 6, 6, 6])
                self.assertEqual(data["shown"][3], 6)

    def test_column_selector_last_column_locked(self):
        data = self.observe_page("""
            const table = document.querySelector(".tasks");
            const panel = document.getElementById("columns-panel");
            const button = document.getElementById("columns-button");
            const byKey = key => panel.querySelector(`input[data-column="${key}"]`);
            button.click();
            for (const key of ["stage", "title", "started", "updated", "note", "next"]) {
              byKey(key).click();
            }
            const bot = byKey("bot");
            observations.single = [[...table.tHead.rows[0].cells].map(node => node.textContent),
              table.querySelector("colgroup").children.length,
              table.tBodies[0].rows[0].cells.length, bot.checked, bot.disabled,
              localStorage.getItem("task-dashboard.columns")];
            bot.click();
            observations.afterDisabledClick = [bot.checked,
              [...table.tHead.rows[0].cells].map(node => node.textContent)];
            bot.checked = false;
            bot.dispatchEvent(new Event("change", {bubbles: true}));
            observations.afterForced = [bot.checked,
              [...table.tHead.rows[0].cells].map(node => node.textContent)];
            byKey("next").click();
            observations.two = [[...table.tHead.rows[0].cells].map(node => node.textContent),
              bot.disabled, byKey("next").checked, byKey("next").disabled];
        """)
        self.assertEqual(data["single"][0], ["봇"])
        self.assertEqual(data["single"][1], 1)
        self.assertEqual(data["single"][2], 1)
        self.assertTrue(data["single"][3])
        self.assertTrue(data["single"][4])
        self.assertEqual(json.loads(data["single"][5]), ["bot"])
        self.assertEqual(data["afterDisabledClick"], [True, ["봇"]])
        self.assertEqual(data["afterForced"], [True, ["봇"]])
        self.assertEqual(data["two"][0], ["봇", "다음 할 일"])
        self.assertFalse(data["two"][1])
        self.assertTrue(data["two"][2])
        self.assertFalse(data["two"][3])

    def test_column_selector_persistence_and_refresh(self):
        first = self.observe_page("""
            const table = document.querySelector(".tasks");
            const panel = document.getElementById("columns-panel");
            const button = document.getElementById("columns-button");
            const headersOf = () => [...table.tHead.rows[0].cells].map(node => node.textContent);
            button.click();
            panel.querySelector('input[data-column="stage"]').click();
            panel.querySelector('input[data-column="next"]').click();
            observations.afterHide = [headersOf(), localStorage.getItem("task-dashboard.columns")];
            document.querySelector('[data-status="완료"]').click();
            observations.afterTab = headersOf();
            document.querySelector('[data-status="전체"]').click();
            await tick();
            observations.afterRefresh = [headersOf(),
              table.tBodies[0].rows[0].cells.length,
              localStorage.getItem("task-dashboard.columns")];
        """)
        # 프로세스 종료 시 localStorage flush에 의존하지 않도록 프로필을
        # 재사용하지 않고 첫 실행에서 읽은 저장값 문자열을 새 브라우저 실행의
        # setup 단계에서 미리 넣는다. extra_setup은 앱 스크립트보다 먼저
        # 실행되므로 첫 페이지 로드 시 복원된다.
        stored = first["afterHide"][1]
        second = self.observe_page("""
            const table = document.querySelector(".tasks");
            const panel = document.getElementById("columns-panel");
            observations.headers = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.cols = [...table.querySelector("colgroup").children].map(node => node.className);
            observations.checked = [...panel.querySelectorAll("input")].map(node => [node.dataset.column, node.checked]);
            observations.stored = localStorage.getItem("task-dashboard.columns");
            observations.panelHidden = panel.hidden;
            observations.rowCells = table.tBodies[0].rows[0].cells.length;
            await tick();
            observations.afterRefresh = [...table.tHead.rows[0].cells].map(node => node.textContent);
        """, extra_setup='try { localStorage.setItem("task-dashboard.columns", ' + json.dumps(stored) + '); } catch (error) {}')
        self.assertEqual(first["afterHide"][0], ["봇", "작업", "시작", "마지막 업데이트", "메모"])
        self.assertEqual(json.loads(first["afterHide"][1]), ["bot", "title", "started", "updated", "note"])
        self.assertEqual(first["afterTab"], first["afterHide"][0])
        self.assertEqual(first["afterRefresh"][0], first["afterHide"][0])
        self.assertEqual(first["afterRefresh"][1], 5)
        self.assertEqual(first["afterRefresh"][2], first["afterHide"][1])
        self.assertEqual(second["headers"], first["afterHide"][0])
        self.assertEqual(second["cols"], ["col-bot", "col-title", "col-time", "col-time", "col-note"])
        self.assertEqual(dict(second["checked"]),
                         {"bot": True, "stage": False, "title": True, "started": True, "updated": True, "note": True, "next": False})
        self.assertEqual(second["stored"], first["afterHide"][1])
        self.assertTrue(second["panelHidden"])
        self.assertEqual(second["rowCells"], 5)
        self.assertEqual(second["afterRefresh"], second["headers"])

    def test_column_selector_corrupted_fallback(self):
        exercise = """
            const table = document.querySelector(".tasks");
            observations.headers = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.cols = table.querySelector("colgroup").children.length;
            observations.rowCells = table.tBodies[0].rows[0].cells.length;
            observations.checked = [...document.querySelectorAll("#columns-panel input")]
              .map(node => node.checked);
        """
        cases = [
            ('try { localStorage.setItem("task-dashboard.columns", "not-json"); } catch (error) {}',
             ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"]),
            ('try { localStorage.setItem("task-dashboard.columns", "[]"); } catch (error) {}',
             ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"]),
            ('try { localStorage.setItem("task-dashboard.columns", JSON.stringify({bot: true})); } catch (error) {}',
             ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"]),
            ('try { localStorage.setItem("task-dashboard.columns", JSON.stringify(["bot", "unknown", 123, null, "note"])); } catch (error) {}',
             ["봇", "메모"]),
            ('try { localStorage.setItem("task-dashboard.columns", JSON.stringify(["stage", "unknown"])); } catch (error) {}',
             ["현재 단계"]),
        ]
        for extra_setup, expected in cases:
            with self.subTest(expected=expected):
                data = self.observe_page(exercise, 1280, extra_setup=extra_setup)
                self.assertEqual(data["headers"], expected)
                self.assertEqual(data["cols"], len(expected))
                self.assertEqual(data["rowCells"], len(expected))
                labels = ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"]
                self.assertEqual(data["checked"], [label in expected for label in labels])
        with self.subTest("storage-throws"):
            data = self.observe_page("""
                const table = document.querySelector(".tasks");
                const panel = document.getElementById("columns-panel");
                const button = document.getElementById("columns-button");
                observations.initial = [...table.tHead.rows[0].cells].map(node => node.textContent);
                button.click();
                panel.querySelector('input[data-column="note"]').click();
                observations.afterHide = [...table.tHead.rows[0].cells].map(node => node.textContent);
            """, 1280, extra_setup="""
                localStorage.getItem = () => { throw new Error("denied"); };
                localStorage.setItem = () => { throw new Error("denied"); };
            """)
            self.assertEqual(data["initial"], ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
            self.assertEqual(data["afterHide"], ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "다음 할 일"])

    def test_column_selector_keyboard_and_outside(self):
        data = self.observe_page("""
            const button = document.getElementById("columns-button");
            const panel = document.getElementById("columns-panel");
            button.focus();
            button.click();
            const first = panel.querySelector("input");
            first.focus();
            observations.opened = [panel.hidden, button.getAttribute("aria-expanded"),
              document.activeElement === first, first.type, first.tabIndex];
            first.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true, cancelable: true}));
            observations.afterEscape = [panel.hidden, button.getAttribute("aria-expanded"),
              document.activeElement.id];
            button.click();
            observations.reopened = [panel.hidden, button.getAttribute("aria-expanded")];
            document.querySelector("h1").click();
            observations.afterOutside = [panel.hidden, button.getAttribute("aria-expanded")];
        """)
        self.assertEqual(data["opened"], [False, "true", True, "checkbox", 0])
        self.assertEqual(data["afterEscape"], [True, "false", "columns-button"])
        self.assertEqual(data["reopened"], [False, "true"])
        self.assertEqual(data["afterOutside"], [True, "false"])

    def test_hidden_task_column_row_focus_and_keyboard_toggle(self):
        data = self.observe_page("""
            const table = document.querySelector(".tasks");
            const button = document.getElementById("columns-button");
            const panel = document.getElementById("columns-panel");
            const byKey = key => panel.querySelector(`input[data-column="${key}"]`);
            observations.headersInitial = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.rowsVisible = [...document.querySelectorAll(".task-row")]
              .map(row => [row.hasAttribute("tabindex"), row.getAttribute("aria-expanded"), row.getAttribute("aria-label")]);
            button.click();
            byKey("title").click();
            observations.headersHidden = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.toggleCountHidden = document.querySelectorAll(".task-toggle").length;
            observations.rowsHidden = [...document.querySelectorAll(".task-row")]
              .map(row => [row.getAttribute("tabindex"), row.tabIndex, row.getAttribute("aria-expanded"), row.getAttribute("aria-label")]);
            let firstRow = document.querySelectorAll(".task-row")[0];
            firstRow.focus();
            observations.focusedAfterTab = [document.activeElement === firstRow,
              document.activeElement?.tagName, document.activeElement?.dataset?.taskId];
            const enterEvent = new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true});
            firstRow.dispatchEvent(enterEvent);
            observations.openedByEnter = [enterEvent.defaultPrevented, table.querySelector(".history") !== null,
              table.querySelector(".history")?.cells[0].colSpan,
              document.activeElement?.tagName, document.activeElement?.dataset?.taskId,
              document.activeElement?.getAttribute?.("aria-expanded")];
            let expandedRow = document.querySelectorAll(".task-row")[0];
            observations.expandedRowAttrs = [expandedRow.getAttribute("aria-expanded"),
              expandedRow.getAttribute("aria-label"), expandedRow.getAttribute("tabindex")];
            const spaceEvent = new KeyboardEvent("keydown", {key: " ", bubbles: true, cancelable: true});
            expandedRow.dispatchEvent(spaceEvent);
            observations.closedBySpace = [spaceEvent.defaultPrevented, table.querySelector(".history") === null,
              document.querySelectorAll(".task-row")[0].getAttribute("aria-expanded"),
              document.activeElement?.tagName, document.activeElement?.dataset?.taskId];
            const child = document.querySelectorAll(".task-row")[0].cells[0];
            const childEvent = new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true});
            child.dispatchEvent(childEvent);
            observations.childIgnored = [table.querySelector(".history") === null, childEvent.defaultPrevented];
            byKey("title").click();
            observations.headersShown = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.toggleCountShown = document.querySelectorAll(".task-toggle").length;
            observations.rowsShown = [...document.querySelectorAll(".task-row")]
              .map(row => [row.hasAttribute("tabindex"), row.getAttribute("aria-expanded"), row.getAttribute("aria-label")]);
        """)
        self.assertIn("작업", data["headersInitial"])
        self.assertEqual(data["rowsVisible"], [[False, None, None]] * 4)
        self.assertNotIn("작업", data["headersHidden"])
        self.assertEqual(len(data["headersHidden"]), 6)
        self.assertEqual(data["toggleCountHidden"], 0)
        self.assertEqual(data["rowsHidden"], [
            ["0", 0, "false", "active 이력 보기"],
            ["0", 0, "false", "blocked 이력 보기"],
            ["0", 0, "false", "unnamed 이력 보기"],
            ["0", 0, "false", "done 이력 보기"],
        ])
        self.assertEqual(data["focusedAfterTab"], [True, "TR", "active"])
        self.assertEqual(data["openedByEnter"], [False, True, 6, "TR", "active", "true"])
        self.assertEqual(data["expandedRowAttrs"], ["true", "active 이력 보기", "0"])
        self.assertEqual(data["closedBySpace"], [True, True, "false", "TR", "active"])
        self.assertEqual(data["childIgnored"], [True, False])
        self.assertIn("작업", data["headersShown"])
        self.assertEqual(data["toggleCountShown"], 4)
        self.assertEqual(data["rowsShown"], [[False, None, None]] * 4)
