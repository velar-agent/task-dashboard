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
            const taskTitleComputed = getComputedStyle(taskTitleNode);
            observations.taskIdStyle = [taskIdComputed.fontFamily, taskIdComputed.fontSize,
              taskIdComputed.color, taskIdComputed.backgroundColor, taskIdComputed.borderRadius,
              taskIdComputed.borderStyle, taskIdComputed.fontWeight, taskIdComputed.borderWidth,
              taskIdComputed.padding, taskIdComputed.letterSpacing,
              taskTitleComputed.fontFamily, taskTitleComputed.fontSize, taskTitleComputed.color,
              taskTitleComputed.fontWeight, taskTitleComputed.letterSpacing];
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
                if width == 1280:
                    self.assertTrue(data["aligned"])
                else:
                    # 640px 이하 카드에서는 thead가 시각적으로 숨겨져
                    # 데스크톱 열 정렬이 성립하지 않는다. 헤더 셀은 DOM에 남는다.
                    self.assertFalse(data["aligned"])
                self.assertEqual(data["sortedBy"], "마지막 업데이트")
                self.assertEqual(data["columns"], [
                    ["봇", "col-bot", "Engineering"], ["현재 단계", "col-stage", "코딩"],
                    ["작업", "col-title", "active기능 구현"], ["시작", "col-time", "2026-11-18 10:00"],
                    ["마지막 업데이트", "col-time", "2026-11-18 12:00"],
                    ["메모", "col-note", "구현 중"], ["다음 할 일", "col-next", "리뷰 요청"],
                ])
                self.assertEqual(data["taskOrder"], ["active", "기능 구현", ["task-id", "task-title"], True, 0, 0])
                font_family, font_size, color, background, radius, border, weight, border_width, padding, spacing, title_family, title_size, title_color, title_weight, title_spacing = data["taskIdStyle"]
                self.assertEqual(weight, "700")
                self.assertEqual(title_weight, "400")
                self.assertEqual(background, "rgba(0, 0, 0, 0)")
                self.assertEqual(border_width, "0px")
                self.assertEqual(border, "none")
                self.assertEqual(radius, "0px")
                self.assertEqual(padding, "0px")
                self.assertNotIn("monospace", font_family.lower())
                self.assertEqual(font_family, title_family)
                self.assertEqual(font_size, title_size)
                self.assertEqual(color, title_color)
                self.assertEqual(spacing, title_spacing)
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
            choose("name:<b>QA</b>");
            observations.qa = record();
            choose("");
            observations.backToAll = record();
            choose("name:Engineering");
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
            choose("name:(미지정)");
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
        self.assertEqual(data["all"], ["", [4, 1, 2, 1], ["active", "blocked", "done", "unnamed"]])
        self.assertEqual(data["qa"], ["name:<b>QA</b>", [1, 0, 0, 1], ["blocked"]])
        self.assertEqual(data["backToAll"], data["all"])
        self.assertEqual(data["engineering"], ["name:Engineering", [2, 1, 1, 0], ["active", "done"]])
        self.assertEqual(data["done"], ["name:Engineering", [2, 1, 1, 0], ["done"]])
        self.assertEqual(data["refresh"], ["name:Engineering", [2, 0, 1, 1], ["done"]])
        self.assertEqual(data["fallback"], ["", [2, 0, 1, 1], ["unnamed"]])
        self.assertCountEqual(data["fallbackOptions"], ["전체", "(미지정)", "<b>QA</b>"])
        self.assertEqual(data["unnamed"], ["name:(미지정)", [1, 0, 1, 0], ["unnamed"]])
        self.assertEqual(data["unnamedCell"], "(미지정)")
        self.assertEqual(data["empty"], ["", [0, 0, 0, 0], []])
        self.assertTrue(data["emptyLoaded"])

    def test_bot_filter_groups_by_id_and_search_finds_old_name(self):
        extra = """
        const ev = (taskId, botId, botName, recorded, ts) => ({task_id: taskId, bot_id: botId, bot_name: botName,
          bot: botName, recorded_bot_name: recorded, title: taskId, stage: "코딩", note: "plain", next: "plain", ts});
        snapshot.tasks = [
          {task_id: "renamed-a", bot_id: "bot-1", bot_name: "NewBot", bot: "IgnoredBot", title: "A", stage: "코딩",
           status: "진행 중", started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T12:00:00+09:00",
           note: "plain", next: "plain", events: [ev("renamed-a", "bot-1", "NewBot", "OldBot", "2026-11-18T12:00:00+09:00")]},
          {task_id: "renamed-b", bot_id: "bot-1", bot_name: "NewBot", bot: "NewBot", title: "B", stage: "코딩",
           status: "진행 중", started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T11:00:00+09:00",
           note: "plain", next: "plain", events: [ev("renamed-b", "bot-1", "NewBot", "NewBot", "2026-11-18T11:00:00+09:00")]},
          {task_id: "other", bot_id: "bot-2", bot_name: "NewBot", bot: "NewBot", title: "C", stage: "코딩",
           status: "진행 중", started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T10:00:00+09:00",
           note: "plain", next: "plain", events: [ev("other", "bot-2", "NewBot", "NewBot", "2026-11-18T10:00:00+09:00")]},
          {task_id: "solo", bot: "Solo", title: "D", stage: "코딩",
           status: "진행 중", started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T09:30:00+09:00",
           note: "plain", next: "plain", events: [{task_id: "solo", bot: "Solo", title: "D", stage: "코딩", note: "plain", next: "plain", ts: "2026-11-18T09:30:00+09:00"}]}
        ];
        """
        data = self.observe_page("""
            const select = document.getElementById("bot-filter");
            const ids = () => [...document.querySelectorAll(".task-toggle")].map(node => node.dataset.taskId);
            const botCellFor = id => {
              const toggle = [...document.querySelectorAll(".task-toggle")].find(node => node.dataset.taskId === id);
              return toggle ? toggle.closest("tr").cells[0].textContent : null;
            };
            observations.options = [...select.options].map(option => [option.value, option.textContent]);
            observations.renamedCell = botCellFor("renamed-a");
            observations.soloCell = botCellFor("solo");
            const choose = value => { select.value = value; select.dispatchEvent(new Event("change")); };
            choose("bot-1");
            observations.bot1Ids = ids();
            choose("bot-2");
            observations.bot2Ids = ids();
            choose("name:Solo");
            observations.soloIds = ids();
            choose("bot-1");
            snapshot.tasks = snapshot.tasks.map(task => task.bot_id === "bot-1"
              ? {...task, bot_name: "RenamedBot", bot: "RenamedBot",
                 events: task.events.map(event => ({...event, bot_name: "RenamedBot", bot: "RenamedBot"}))}
              : task);
            await tick();
            observations.afterRename = {
              value: select.value,
              options: [...select.options].map(option => [option.value, option.textContent]),
              ids: ids(),
              cell: botCellFor("renamed-a"),
            };
            const searchEl = document.getElementById("search");
            const applySearch = query => {
              searchEl.value = query;
              searchEl.dispatchEvent(new Event("input", {bubbles: true}));
              searchEl.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
            };
            choose("");
            applySearch("OldBot");
            observations.oldNameIds = ids();
            applySearch("bot-1");
            observations.idSearchIds = ids();
            applySearch("RenamedBot");
            observations.currentNameIds = ids();
        """, 1280, extra_setup=extra)
        self.assertEqual(data["options"], [
            ["", "전체"],
            ["bot-1", "NewBot"],
            ["bot-2", "NewBot"],
            ["name:Solo", "Solo"],
        ])
        self.assertEqual(data["renamedCell"], "NewBot")
        self.assertEqual(data["soloCell"], "Solo")
        self.assertEqual(data["bot1Ids"], ["renamed-a", "renamed-b"])
        self.assertEqual(data["bot2Ids"], ["other"])
        self.assertEqual(data["soloIds"], ["solo"])
        self.assertEqual(data["afterRename"]["value"], "bot-1")
        self.assertEqual(data["afterRename"]["ids"], ["renamed-a", "renamed-b"])
        self.assertEqual(data["afterRename"]["cell"], "RenamedBot")
        self.assertIn(["bot-1", "RenamedBot"], data["afterRename"]["options"])
        self.assertEqual(data["oldNameIds"], ["renamed-a"])
        self.assertEqual(data["idSearchIds"], ["renamed-a", "renamed-b"])
        self.assertEqual(data["currentNameIds"], ["renamed-a", "renamed-b"])

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
            ["0", 0, "false", "done 이력 보기"],
            ["0", 0, "false", "unnamed 이력 보기"],
        ])
        self.assertEqual(data["focusedAfterTab"], [True, "TR", "active"])
        self.assertEqual(data["openedByEnter"], [False, True, 6, "TR", "active", "true"])
        self.assertEqual(data["expandedRowAttrs"], ["true", "active 이력 보기", "0"])
        self.assertEqual(data["closedBySpace"], [True, True, "false", "TR", "active"])
        self.assertEqual(data["childIgnored"], [True, False])
        self.assertIn("작업", data["headersShown"])
        self.assertEqual(data["toggleCountShown"], 4)
        self.assertEqual(data["rowsShown"], [[False, None, None]] * 4)

    def test_column_order_panel_move_focus_and_reset(self):
        exercise = """
            const table = document.querySelector(".tasks");
            const button = document.getElementById("columns-button");
            const panel = document.getElementById("columns-panel");
            const headersOf = () => [...table.tHead.rows[0].cells].map(node => node.textContent);
            const colsOf = () => [...table.querySelector("colgroup").children].map(node => node.className);
            const firstCellsOf = () => [...table.tBodies[0].rows[0].cells].map(node => node.textContent);
            const rowCounts = () => [...table.querySelectorAll(".task-row")].map(row => row.cells.length);
            const panelKeys = () => [...panel.querySelectorAll(".column-item")].map(node => node.dataset.column);
            const moveButton = (key, direction) => panel.querySelector(
              `.column-item[data-column="${key}"] button[data-direction="${direction}"]`);
            const reset = () => panel.querySelector(".reset-order");
            observations.idWeight = getComputedStyle(document.querySelector(".task-id")).fontWeight;
            observations.titleWeight = getComputedStyle(document.querySelector(".task-title")).fontWeight;
            const idComputed = getComputedStyle(document.querySelector(".task-id"));
            const titleComputed = getComputedStyle(document.querySelector(".task-title"));
            observations.idStyle = [idComputed.fontFamily, idComputed.fontSize,
              idComputed.color, idComputed.backgroundColor, idComputed.borderRadius,
              idComputed.borderStyle, idComputed.fontWeight, idComputed.borderWidth,
              idComputed.padding, idComputed.letterSpacing,
              titleComputed.fontFamily, titleComputed.fontSize, titleComputed.color,
              titleComputed.fontWeight, titleComputed.letterSpacing];
            observations.initial = [panelKeys(), headersOf(), colsOf(), firstCellsOf(), rowCounts(),
              moveButton("bot", "up").disabled, moveButton("next", "down").disabled,
              moveButton("bot", "down").disabled, moveButton("next", "up").disabled,
              moveButton("bot", "up").getAttribute("aria-label"),
              moveButton("bot", "down").getAttribute("aria-label"),
              moveButton("bot", "up").textContent, moveButton("bot", "down").textContent,
              [reset().disabled, reset().textContent, reset().getAttribute("type")]];
            const toggle = () => [...document.querySelectorAll(".task-toggle")]
              .find(node => node.dataset.taskId === "active");
            toggle().focus();
            toggle().click();
            observations.historyOpen = [table.querySelector(".history")?.cells[0].colSpan,
              document.activeElement?.dataset?.taskId];
            button.click();
            moveButton("bot", "down").click();
            observations.afterDown = [panelKeys(), headersOf(), colsOf(), firstCellsOf(), rowCounts(),
              panel.hidden, button.getAttribute("aria-expanded"),
              document.activeElement?.getAttribute?.("aria-label"),
              document.activeElement?.dataset?.direction,
              localStorage.getItem("task-dashboard.column-order"), reset().disabled,
              table.querySelector(".history")?.cells[0].colSpan];
            moveButton("bot", "down").click();
            observations.afterDown2 = [panelKeys(), headersOf(),
              document.activeElement?.getAttribute?.("aria-label"),
              document.activeElement?.dataset?.direction];
            moveButton("bot", "up").click();
            moveButton("bot", "up").click();
            observations.afterUpToTop = [panelKeys(), headersOf(),
              document.activeElement?.getAttribute?.("aria-label"),
              document.activeElement?.dataset?.direction,
              moveButton("bot", "up").disabled, moveButton("bot", "down").disabled];
            moveButton("bot", "down").click();
            observations.beforeReset = [panelKeys(), headersOf(), reset().disabled,
              document.activeElement?.getAttribute?.("aria-label")];
            reset().click();
            observations.afterReset = [panelKeys(), headersOf(), panel.hidden,
              button.getAttribute("aria-expanded"), reset().disabled,
              localStorage.getItem("task-dashboard.column-order")];
            const rect = panel.getBoundingClientRect();
            const moveRect = moveButton("bot", "down").getBoundingClientRect();
            observations.noOverflow = [document.documentElement.scrollWidth <= window.innerWidth,
              document.body.scrollWidth <= window.innerWidth, rect.left >= -1,
              rect.right <= window.innerWidth + 1, window.innerWidth - rect.right >= -1];
            observations.moveSize = [moveRect.width, moveRect.height];
        """
        for width in (1280, 390):
            with self.subTest(width=width):
                data = self.observe_page(exercise, width)
                self.assertEqual(data["idWeight"], "700")
                self.assertEqual(data["titleWeight"], "400")
                font_family, font_size, color, background, radius, border, weight, border_width, padding, spacing, title_family, title_size, title_color, title_weight, title_spacing = data["idStyle"]
                self.assertEqual(weight, "700")
                self.assertEqual(title_weight, "400")
                self.assertEqual(background, "rgba(0, 0, 0, 0)")
                self.assertEqual(border_width, "0px")
                self.assertEqual(border, "none")
                self.assertEqual(radius, "0px")
                self.assertEqual(padding, "0px")
                self.assertNotIn("monospace", font_family.lower())
                self.assertEqual(font_family, title_family)
                self.assertEqual(font_size, title_size)
                self.assertEqual(color, title_color)
                self.assertEqual(spacing, title_spacing)
                default = ["bot", "stage", "title", "started", "updated", "note", "next"]
                moved = ["stage", "bot", "title", "started", "updated", "note", "next"]
                moved2 = ["stage", "title", "bot", "started", "updated", "note", "next"]
                default_labels = ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"]
                self.assertEqual(data["initial"][0], default)
                self.assertEqual(data["initial"][1], default_labels)
                self.assertEqual(data["initial"][2],
                                 ["col-bot", "col-stage", "col-title", "col-time", "col-time", "col-note", "col-next"])
                self.assertEqual(data["initial"][3],
                                 ["Engineering", "코딩", "active기능 구현", "2026-11-18 10:00",
                                  "2026-11-18 12:00", "구현 중", "리뷰 요청"])
                self.assertEqual(data["initial"][4], [7, 7, 7, 7])
                self.assertEqual(data["initial"][5:9], [True, True, False, False])
                self.assertEqual(data["initial"][9:13], ["봇 위로 이동", "봇 아래로 이동", "▲", "▼"])
                self.assertEqual(data["initial"][13], [True, "기본 순서로", "button"])
                self.assertEqual(data["historyOpen"], [7, "active"])
                self.assertEqual(data["afterDown"][0], moved)
                self.assertEqual(data["afterDown"][1],
                                 ["현재 단계", "봇", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
                self.assertEqual(data["afterDown"][2],
                                 ["col-stage", "col-bot", "col-title", "col-time", "col-time", "col-note", "col-next"])
                self.assertEqual(data["afterDown"][3],
                                 ["코딩", "Engineering", "active기능 구현", "2026-11-18 10:00",
                                  "2026-11-18 12:00", "구현 중", "리뷰 요청"])
                self.assertEqual(data["afterDown"][4], [7, 7, 7, 7])
                self.assertEqual(data["afterDown"][5:9], [False, "true", "봇 아래로 이동", "down"])
                self.assertEqual(json.loads(data["afterDown"][9]), moved)
                self.assertFalse(data["afterDown"][10])
                self.assertEqual(data["afterDown"][11], 7)
                self.assertEqual(data["afterDown2"][0], moved2)
                self.assertEqual(data["afterDown2"][1],
                                 ["현재 단계", "작업", "봇", "시작", "마지막 업데이트", "메모", "다음 할 일"])
                self.assertEqual(data["afterDown2"][2:4], ["봇 아래로 이동", "down"])
                self.assertEqual(data["afterUpToTop"][0], default)
                self.assertEqual(data["afterUpToTop"][1], default_labels)
                self.assertEqual(data["afterUpToTop"][2:6], ["봇 아래로 이동", "down", True, False])
                self.assertEqual(data["beforeReset"][0], moved)
                self.assertFalse(data["beforeReset"][2])
                self.assertEqual(data["beforeReset"][3], "봇 아래로 이동")
                self.assertEqual(data["afterReset"][0], default)
                self.assertEqual(data["afterReset"][1], default_labels)
                self.assertEqual(data["afterReset"][2:5], [False, "true", True])
                self.assertEqual(json.loads(data["afterReset"][5]), default)
                self.assertTrue(all(data["noOverflow"]))
                self.assertGreaterEqual(data["moveSize"][0], 28)
                self.assertGreaterEqual(data["moveSize"][1], 28)

    def test_column_order_persistence_and_correction(self):
        first = self.observe_page("""
            const table = document.querySelector(".tasks");
            const panel = document.getElementById("columns-panel");
            const button = document.getElementById("columns-button");
            const headersOf = () => [...table.tHead.rows[0].cells].map(node => node.textContent);
            const panelKeys = () => [...panel.querySelectorAll(".column-item")].map(node => node.dataset.column);
            const moveButton = (key, direction) => panel.querySelector(
              `.column-item[data-column="${key}"] button[data-direction="${direction}"]`);
            button.click();
            moveButton("bot", "down").click();
            observations.afterOrder = [headersOf(), panelKeys(),
              localStorage.getItem("task-dashboard.column-order"),
              localStorage.getItem("task-dashboard.columns")];
            panel.querySelector('input[data-column="stage"]').click();
            observations.afterHide = [headersOf(), panelKeys(),
              localStorage.getItem("task-dashboard.column-order"),
              localStorage.getItem("task-dashboard.columns")];
            document.querySelector('[data-status="완료"]').click();
            observations.afterTab = headersOf();
            document.querySelector('[data-status="전체"]').click();
            await tick();
            observations.afterRefresh = [headersOf(),
              localStorage.getItem("task-dashboard.column-order")];
            panel.querySelector('input[data-column="note"]').click();
            snapshot = {tasks: [], generated_at: null};
            await tick();
            observations.empty = [document.querySelector("td.empty")?.colSpan, headersOf()];
        """)
        stored_order = first["afterOrder"][2]
        second = self.observe_page("""
            const table = document.querySelector(".tasks");
            const panel = document.getElementById("columns-panel");
            observations.headers = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.cols = [...table.querySelector("colgroup").children].map(node => node.className);
            observations.panelKeys = [...panel.querySelectorAll(".column-item")].map(node => node.dataset.column);
            observations.checked = [...panel.querySelectorAll("input")].map(node => [node.dataset.column, node.checked]);
            observations.stored = localStorage.getItem("task-dashboard.column-order");
            observations.panelHidden = panel.hidden;
            observations.rowCells = table.tBodies[0].rows[0].cells.length;
            await tick();
            observations.afterRefresh = [...table.tHead.rows[0].cells].map(node => node.textContent);
        """, extra_setup='try { localStorage.setItem("task-dashboard.column-order", '
                      + json.dumps(stored_order) + '); } catch (error) {}')
        moved = ["stage", "bot", "title", "started", "updated", "note", "next"]
        self.assertEqual(first["afterOrder"][0],
                         ["현재 단계", "봇", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertEqual(first["afterOrder"][1], moved)
        self.assertEqual(json.loads(first["afterOrder"][2]), moved)
        self.assertIsNone(first["afterOrder"][3])
        self.assertEqual(first["afterHide"][0], ["봇", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertEqual(first["afterHide"][1], moved)
        self.assertEqual(json.loads(first["afterHide"][2]), moved)
        self.assertEqual(json.loads(first["afterHide"][3]),
                         ["bot", "title", "started", "updated", "note", "next"])
        self.assertEqual(first["afterTab"], first["afterHide"][0])
        self.assertEqual(first["afterRefresh"][0], first["afterHide"][0])
        self.assertEqual(json.loads(first["afterRefresh"][1]), moved)
        self.assertEqual(first["empty"], [5, ["봇", "작업", "시작", "마지막 업데이트", "다음 할 일"]])
        self.assertEqual(second["headers"],
                         ["현재 단계", "봇", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertEqual(second["cols"],
                         ["col-stage", "col-bot", "col-title", "col-time", "col-time", "col-note", "col-next"])
        self.assertEqual(second["panelKeys"], moved)
        self.assertEqual(dict(second["checked"]),
                         {"bot": True, "stage": True, "title": True, "started": True, "updated": True, "note": True, "next": True})
        self.assertEqual(second["stored"], stored_order)
        self.assertTrue(second["panelHidden"])
        self.assertEqual(second["rowCells"], 7)
        self.assertEqual(second["afterRefresh"], second["headers"])

        default_labels = ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"]
        exercise = """
            const table = document.querySelector(".tasks");
            const panel = document.getElementById("columns-panel");
            observations.headers = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.panelKeys = [...panel.querySelectorAll(".column-item")].map(node => node.dataset.column);
            observations.cols = [...table.querySelector("colgroup").children].map(node => node.className);
            observations.rowCells = table.tBodies[0].rows[0].cells.length;
        """
        cases = [
            ('try { localStorage.setItem("task-dashboard.column-order", "not-json"); } catch (error) {}',
             default_labels, ["bot", "stage", "title", "started", "updated", "note", "next"]),
            ('try { localStorage.setItem("task-dashboard.column-order", "[]"); } catch (error) {}',
             default_labels, ["bot", "stage", "title", "started", "updated", "note", "next"]),
            ('try { localStorage.setItem("task-dashboard.column-order", JSON.stringify({bot: true})); } catch (error) {}',
             default_labels, ["bot", "stage", "title", "started", "updated", "note", "next"]),
            ('try { localStorage.setItem("task-dashboard.column-order", JSON.stringify(["unknown", 123])); } catch (error) {}',
             default_labels, ["bot", "stage", "title", "started", "updated", "note", "next"]),
            ('try { localStorage.setItem("task-dashboard.column-order", JSON.stringify(["next", "unknown", "bot", "next", 123, "stage"])); } catch (error) {}',
             ["다음 할 일", "봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모"],
             ["next", "bot", "stage", "title", "started", "updated", "note"]),
        ]
        for extra_setup, expected_labels, expected_keys in cases:
            with self.subTest(expected=expected_keys):
                data = self.observe_page(exercise, 1280, extra_setup=extra_setup)
                self.assertEqual(data["headers"], expected_labels)
                self.assertEqual(data["panelKeys"], expected_keys)
                self.assertEqual(data["cols"], ["col-bot", "col-stage", "col-title", "col-time",
                                                "col-time", "col-note", "col-next"]
                                 if expected_keys[0] == "bot" else
                                 ["col-next", "col-bot", "col-stage", "col-title", "col-time",
                                  "col-time", "col-note"])
                self.assertEqual(data["rowCells"], 7)
        data = self.observe_page("""
            const table = document.querySelector(".tasks");
            const panel = document.getElementById("columns-panel");
            const button = document.getElementById("columns-button");
            const headersOf = () => [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.initial = headersOf();
            button.click();
            panel.querySelector('.column-item[data-column="bot"] button[data-direction="down"]').click();
            observations.afterMove = headersOf();
        """, 1280, extra_setup="""
            const orderGet = localStorage.getItem.bind(localStorage);
            const orderSet = localStorage.setItem.bind(localStorage);
            localStorage.getItem = key => {
              if (key === "task-dashboard.column-order") throw new Error("denied");
              return orderGet(key);
            };
            localStorage.setItem = function(key, value) {
              if (key === "task-dashboard.column-order") throw new Error("denied");
              return orderSet(key, value);
            };
        """)
        self.assertEqual(data["initial"], default_labels)
        self.assertEqual(data["afterMove"],
                         ["현재 단계", "봇", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])

    def test_column_order_header_drag_and_keyboard(self):
        data = self.observe_page("""
            const table = document.querySelector(".tasks");
            const headersOf = () => [...table.tHead.rows[0].cells].map(node => node.textContent);
            const byLabel = text => [...table.tHead.rows[0].cells].find(node => node.textContent === text);
            observations.attrs = [byLabel("봇").draggable, byLabel("봇").tabIndex,
              byLabel("봇").getAttribute("aria-label"),
              byLabel("봇").getAttribute("aria-description"),
              byLabel("마지막 업데이트").getAttribute("aria-sort"),
              byLabel("봇").getAttribute("tabindex")];
            observations.sorted = table.querySelector('th[aria-sort="descending"]').textContent;
            const botTh = byLabel("봇");
            botTh.focus();
            observations.focused = document.activeElement === botTh;
            const leftEvent = new KeyboardEvent("keydown", {key: "ArrowLeft", altKey: true, bubbles: true, cancelable: true});
            botTh.dispatchEvent(leftEvent);
            observations.atLeftEdge = [headersOf(), leftEvent.defaultPrevented,
              document.activeElement?.textContent, document.activeElement?.tagName];
            const plainEvent = new KeyboardEvent("keydown", {key: "ArrowRight", bubbles: true, cancelable: true});
            document.activeElement.dispatchEvent(plainEvent);
            observations.plain = [headersOf(), plainEvent.defaultPrevented];
            const rightEvent = new KeyboardEvent("keydown", {key: "ArrowRight", altKey: true, bubbles: true, cancelable: true});
            document.activeElement.dispatchEvent(rightEvent);
            observations.afterRight = [headersOf(), rightEvent.defaultPrevented,
              document.activeElement?.textContent, document.activeElement?.tagName,
              localStorage.getItem("task-dashboard.column-order")];
            const source = byLabel("작업");
            const target = byLabel("메모");
            const transfer = {value: "", setData(kind, value) { this.value = value; },
              getData() { return this.value; }, effectAllowed: "", dropEffect: ""};
            const fire = (type, node) => {
              const event = new Event(type, {bubbles: true, cancelable: true});
              event.dataTransfer = transfer;
              node.dispatchEvent(event);
              return event;
            };
            const startEvent = fire("dragstart", source);
            observations.dragging = [source.classList.contains("dragging"), transfer.value];
            const overEvent = fire("dragover", target);
            observations.over = [overEvent.defaultPrevented, target.classList.contains("drop-target")];
            const dropEvent = fire("drop", target);
            observations.afterDrop = [headersOf(), dropEvent.defaultPrevented,
              document.activeElement?.textContent, document.activeElement?.tagName,
              localStorage.getItem("task-dashboard.column-order"),
              table.querySelector('th[aria-sort="descending"]').textContent];
            // 드롭 없이 dragend: dragend는 소스 th에서만 발생하므로 대상의
            // .drop-target 잔류를 확인한다. 대상에 dragleave를 보내지 않는다.
            const cancelSource = byLabel("봇");
            const cancelTarget = byLabel("메모");
            fire("dragstart", cancelSource);
            fire("dragover", cancelTarget);
            observations.beforeCancel = [cancelSource.classList.contains("dragging"),
              cancelTarget.classList.contains("drop-target"),
              table.querySelectorAll(".drop-target").length];
            fire("dragend", cancelSource);
            observations.afterCancel = [cancelSource.classList.contains("dragging"),
              cancelTarget.classList.contains("drop-target"),
              table.querySelectorAll(".drop-target").length,
              headersOf()];
            fire("dragstart", cancelSource);
            fire("dragover", cancelTarget);
            fire("dragleave", cancelTarget);
            observations.afterLeave = table.querySelectorAll(".drop-target").length;
            fire("dragend", cancelSource);
            observations.afterLeaveEnd = table.querySelectorAll(".drop-target").length;
        """)
        self.assertEqual(data["attrs"], [True, 0, "봇",
            "드래그하거나 Alt+왼쪽/오른쪽 화살표 키로 열 순서를 이동합니다.", "descending", "0"])
        self.assertEqual(data["sorted"], "마지막 업데이트")
        self.assertTrue(data["focused"])
        default_labels = ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"]
        self.assertEqual(data["atLeftEdge"][0], default_labels)
        self.assertTrue(data["atLeftEdge"][1])
        self.assertEqual(data["atLeftEdge"][2:], ["봇", "TH"])
        self.assertEqual(data["plain"], [default_labels, False])
        moved = ["stage", "bot", "title", "started", "updated", "note", "next"]
        self.assertEqual(data["afterRight"][0],
                         ["현재 단계", "봇", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertTrue(data["afterRight"][1])
        self.assertEqual(data["afterRight"][2:4], ["봇", "TH"])
        self.assertEqual(json.loads(data["afterRight"][4]), moved)
        self.assertEqual(data["dragging"], [True, "title"])
        self.assertEqual(data["over"], [True, True])
        dropped = ["stage", "bot", "started", "updated", "title", "note", "next"]
        self.assertEqual(data["afterDrop"][0],
                         ["현재 단계", "봇", "시작", "마지막 업데이트", "작업", "메모", "다음 할 일"])
        self.assertTrue(data["afterDrop"][1])
        self.assertEqual(data["afterDrop"][2:4], ["작업", "TH"])
        self.assertEqual(json.loads(data["afterDrop"][4]), dropped)
        self.assertEqual(data["afterDrop"][5], "마지막 업데이트")
        self.assertEqual(data["beforeCancel"], [True, True, 1])
        self.assertEqual(data["afterCancel"][:3], [False, False, 0])
        self.assertEqual(data["afterCancel"][3], data["afterDrop"][0])
        self.assertEqual(data["afterLeave"], 0)
        self.assertEqual(data["afterLeaveEnd"], 0)

    def test_column_order_hidden_and_task_positions(self):
        data = self.observe_page("""
            const table = document.querySelector(".tasks");
            const button = document.getElementById("columns-button");
            const panel = document.getElementById("columns-panel");
            const headersOf = () => [...table.tHead.rows[0].cells].map(node => node.textContent);
            const panelKeys = () => [...panel.querySelectorAll(".column-item")].map(node => node.dataset.column);
            const moveButton = (key, direction) => panel.querySelector(
              `.column-item[data-column="${key}"] button[data-direction="${direction}"]`);
            const byKey = key => panel.querySelector(`input[data-column="${key}"]`);
            const byLabel = text => [...table.tHead.rows[0].cells].find(node => node.textContent === text);
            button.click();
            byKey("stage").click();
            observations.afterHide = [headersOf(), panelKeys()];
            moveButton("stage", "down").click();
            observations.afterHiddenMove = [headersOf(), panelKeys(),
              localStorage.getItem("task-dashboard.column-order")];
            byKey("stage").click();
            observations.afterShow = [headersOf(), panelKeys()];
            byKey("stage").click();
            const titleTh = byLabel("작업");
            titleTh.focus();
            const altEvent = new KeyboardEvent("keydown", {key: "ArrowRight", altKey: true, bubbles: true, cancelable: true});
            titleTh.dispatchEvent(altEvent);
            observations.afterAlt = [headersOf(), panelKeys(), panelKeys().length,
              document.activeElement?.textContent, document.activeElement?.tagName];
            byKey("stage").click();
            observations.afterShow2 = headersOf();
            moveButton("title", "up").click();
            moveButton("title", "up").click();
            observations.titlePos = [headersOf(), panelKeys()];
            const firstRow = document.querySelectorAll(".task-row")[0];
            observations.togglePos = [firstRow.cells[1].querySelector(".task-toggle")?.dataset?.taskId,
              firstRow.cells[0].textContent];
            const toggle = firstRow.cells[1].querySelector(".task-toggle");
            toggle.focus();
            toggle.click();
            observations.toggled = [table.querySelector(".history") !== null,
              table.querySelector(".history")?.cells[0].colSpan,
              document.activeElement?.dataset?.taskId,
              document.activeElement?.getAttribute?.("aria-expanded")];
            const toggle2 = () => [...document.querySelectorAll(".task-toggle")]
              .find(node => node.dataset.taskId === "active");
            toggle2().click();
            observations.reclosed = table.querySelector(".history") === null;
            button.click();
            byKey("title").click();
            observations.titleHidden = [headersOf(), panelKeys(), document.querySelectorAll(".task-toggle").length];
            let row = document.querySelectorAll(".task-row")[0];
            row.focus();
            observations.rowFocus = [document.activeElement === row, row.getAttribute("tabindex")];
            row.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
            observations.rowOpen = [table.querySelector(".history") !== null,
              table.querySelector(".history")?.cells[0].colSpan,
              document.activeElement?.dataset?.taskId, document.activeElement?.tagName];
            row = document.querySelectorAll(".task-row")[0];
            const spaceEvent = new KeyboardEvent("keydown", {key: " ", bubbles: true, cancelable: true});
            row.dispatchEvent(spaceEvent);
            observations.rowClose = [table.querySelector(".history") === null, spaceEvent.defaultPrevented,
              document.activeElement?.dataset?.taskId, document.activeElement?.tagName];
        """)
        self.assertEqual(data["afterHide"],
            [["봇", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"],
             ["bot", "stage", "title", "started", "updated", "note", "next"]])
        self.assertEqual(data["afterHiddenMove"][0], data["afterHide"][0])
        self.assertEqual(data["afterHiddenMove"][1],
                         ["bot", "title", "stage", "started", "updated", "note", "next"])
        self.assertEqual(json.loads(data["afterHiddenMove"][2]),
                         ["bot", "title", "stage", "started", "updated", "note", "next"])
        self.assertEqual(data["afterShow"][0],
                         ["봇", "작업", "현재 단계", "시작", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertEqual(data["afterAlt"][0],
                         ["봇", "시작", "작업", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertEqual(data["afterAlt"][1],
                         ["bot", "started", "stage", "title", "updated", "note", "next"])
        self.assertEqual(data["afterAlt"][2], 7)
        self.assertEqual(data["afterAlt"][3:], ["작업", "TH"])
        self.assertEqual(data["afterShow2"],
                         ["봇", "시작", "현재 단계", "작업", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertEqual(data["titlePos"][0],
                         ["봇", "작업", "시작", "현재 단계", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertEqual(data["titlePos"][1],
                         ["bot", "title", "started", "stage", "updated", "note", "next"])
        self.assertEqual(data["togglePos"], ["active", "Engineering"])
        self.assertEqual(data["toggled"], [True, 7, "active", "true"])
        self.assertTrue(data["reclosed"])
        self.assertEqual(data["titleHidden"][0],
                         ["봇", "시작", "현재 단계", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertEqual(data["titleHidden"][1],
                         ["bot", "title", "started", "stage", "updated", "note", "next"])
        self.assertEqual(data["titleHidden"][2], 0)
        self.assertEqual(data["rowFocus"], [True, "0"])
        self.assertEqual(data["rowOpen"], [True, 6, "active", "TR"])
        self.assertEqual(data["rowClose"], [True, True, "active", "TR"])

    def test_mobile_cards_block_display_and_no_overflow(self):
        # Headless Chrome enforces a min window width of 500px, so
        # --window-size=390 gives innerWidth 500. 500 is still <=640,
        # so the card layout is exercised. We assert via matchMedia.
        exercise = """
            const table = document.querySelector(".tasks");
            observations.innerWidth = window.innerWidth;
            observations.is640 = matchMedia("(max-width: 640px)").matches;
            observations.docScroll = document.documentElement.scrollWidth;
            observations.bodyScroll = document.body.scrollWidth;
            observations.noOverflow = document.documentElement.scrollWidth <= window.innerWidth
              && document.body.scrollWidth <= window.innerWidth;
            const row = table.tBodies[0].rows[0];
            observations.tdDisplay = getComputedStyle(row.cells[0]).display;
            observations.tableDisplay = getComputedStyle(table).display;
            observations.theadDisplay = getComputedStyle(table.tHead).display;
            observations.theadClipPath = getComputedStyle(table.tHead).clipPath;
            observations.theadClip = getComputedStyle(table.tHead).clip;
            observations.theadInDom = table.tHead !== null && table.tHead.rows[0].cells.length === 7;
            observations.scrollOverflowX = getComputedStyle(document.querySelector(".scroll")).overflowX;
            snapshot.tasks = [{task_id: "a".repeat(80), bot: "B".repeat(60),
              title: "긴 제목", stage: "코딩", status: "진행 중",
              started: stamp, updated: stamp, note: "n".repeat(120), next: "m".repeat(120), events: []}];
            await tick();
            observations.afterLong = {
              noOverflow: document.documentElement.scrollWidth <= window.innerWidth
                && document.body.scrollWidth <= window.innerWidth,
              tdDisplay: getComputedStyle(table.tBodies[0].rows[0].cells[0]).display,
              scrollWidth: document.documentElement.scrollWidth,
            };
        """
        data = self.observe_page(exercise, 390)
        self.assertTrue(data["is640"])
        self.assertTrue(data["noOverflow"])
        self.assertEqual(data["tdDisplay"], "flex")
        self.assertEqual(data["tableDisplay"], "block")
        # thead stays in DOM for a11y/drag code, visually hidden via clip.
        self.assertTrue(data["theadInDom"])
        self.assertNotEqual(data["theadDisplay"], "none")
        self.assertIn("inset(50%)", data["theadClipPath"] + " " + (data["theadClip"] or ""))
        self.assertNotEqual(data["scrollOverflowX"], "auto")
        self.assertTrue(data["afterLong"]["noOverflow"])
        self.assertEqual(data["afterLong"]["tdDisplay"], "flex")
        self.assertLessEqual(data["afterLong"]["scrollWidth"], data["innerWidth"])

    def test_mobile_title_cell_order_first(self):
        exercise = """
            const table = document.querySelector(".tasks");
            const row = table.tBodies[0].rows[0];
            const titleCell = row.querySelector('td[data-column="title"]')
              || row.querySelector("td.title-cell")
              || [...row.cells].find(td => td.querySelector(".task-toggle"));
            observations.is640 = matchMedia("(max-width: 640px)").matches;
            observations.titleInfo = [titleCell.dataset.column, titleCell.className,
              !!titleCell.querySelector(".task-toggle"),
              getComputedStyle(titleCell).order, getComputedStyle(titleCell).display];
            observations.domOrderBefore = [...row.cells].map(td => td.dataset.column);
            const rects = [...row.cells].map(td => [td.dataset.column, td.getBoundingClientRect().top]);
            rects.sort((a, b) => a[1] - b[1]);
            observations.visualFirstBefore = rects[0][0];
            const button = document.getElementById("columns-button");
            const panel = document.getElementById("columns-panel");
            const moveButton = (key, direction) => panel.querySelector(
              `.column-item[data-column="${key}"] button[data-direction="${direction}"]`);
            button.click();
            moveButton("title", "down").click();
            moveButton("title", "down").click();
            moveButton("title", "down").click();
            const rowAfter = table.tBodies[0].rows[0];
            const titleAfter = rowAfter.querySelector('td[data-column="title"]')
              || rowAfter.querySelector("td.title-cell")
              || [...rowAfter.cells].find(td => td.querySelector(".task-toggle"));
            observations.domOrderAfter = [...rowAfter.cells].map(td => td.dataset.column);
            observations.titleAfterInfo = [titleAfter.dataset.column, titleAfter.className,
              !!titleAfter.querySelector(".task-toggle"),
              getComputedStyle(titleAfter).order, getComputedStyle(titleAfter).display];
            const rectsAfter = [...rowAfter.cells].map(td => [td.dataset.column, td.getBoundingClientRect().top]);
            rectsAfter.sort((a, b) => a[1] - b[1]);
            observations.visualFirstAfter = rectsAfter[0][0];
            const titleTop = titleAfter.getBoundingClientRect().top;
            const minOtherTop = Math.min(...[...rowAfter.cells].filter(td => td !== titleAfter)
              .map(td => td.getBoundingClientRect().top));
            observations.titleOnTop = titleTop <= minOtherTop + 1;
        """
        data = self.observe_page(exercise, 390)
        self.assertTrue(data["is640"])
        self.assertEqual(data["titleInfo"][0], "title")
        self.assertIn("title-cell", data["titleInfo"][1])
        self.assertTrue(data["titleInfo"][2])
        self.assertEqual(data["titleInfo"][3], "-1")
        self.assertEqual(data["titleInfo"][4], "block")
        self.assertEqual(data["visualFirstBefore"], "title")
        self.assertNotEqual(data["domOrderAfter"][0], "title")
        self.assertEqual(data["titleAfterInfo"][0], "title")
        self.assertIn("title-cell", data["titleAfterInfo"][1])
        self.assertTrue(data["titleAfterInfo"][2])
        self.assertEqual(data["titleAfterInfo"][3], "-1")
        self.assertEqual(data["visualFirstAfter"], "title")
        self.assertTrue(data["titleOnTop"])

    def test_mobile_data_labels_respect_order_and_visibility(self):
        exercise = """
            const table = document.querySelector(".tasks");
            const button = document.getElementById("columns-button");
            const panel = document.getElementById("columns-panel");
            const labelsOf = () => [...table.tBodies[0].rows[0].cells].map(td => td.dataset.label);
            const expectedOf = () => [...table.tHead.rows[0].cells].map(th => th.textContent);
            observations.initialLabels = labelsOf();
            observations.initialExpected = expectedOf();
            button.click();
            panel.querySelector('input[data-column="stage"]').click();
            observations.afterHideLabels = labelsOf();
            observations.afterHideExpected = expectedOf();
            panel.querySelector('.column-item[data-column="bot"] button[data-direction="down"]').click();
            observations.afterMoveLabels = labelsOf();
            observations.afterMoveExpected = expectedOf();
            observations.allHaveLabel = [...table.tBodies[0].rows[0].cells].every(td => typeof td.dataset.label === "string" && td.dataset.label.length > 0);
        """
        data = self.observe_page(exercise, 390)
        self.assertEqual(data["initialLabels"], data["initialExpected"])
        self.assertEqual(data["initialLabels"],
                         ["봇", "현재 단계", "작업", "시작", "마지막 업데이트", "메모", "다음 할 일"])
        self.assertEqual(data["afterHideLabels"], data["afterHideExpected"])
        self.assertNotIn("현재 단계", data["afterHideLabels"])
        self.assertEqual(data["afterMoveLabels"], data["afterMoveExpected"])
        self.assertTrue(data["allHaveLabel"])

    def test_mobile_history_data_labels(self):
        exercise = """
            const table = document.querySelector(".tasks");
            snapshot.tasks = snapshot.tasks.map(entry => entry.task_id === "active"
              ? {...entry, events: [
                  {ts: stamp, stage: "계획", note: "첫 메모", next: "다음 점검"},
                  {ts: stamp, stage: "코딩", note: "둘째 메모", next: "셋째 할 일"}]}
              : entry);
            await tick();
            const toggle = [...document.querySelectorAll(".task-toggle")]
              .find(node => node.dataset.taskId === "active");
            toggle.click();
            const history = table.querySelector(".task-row + .history");
            const histTable = history.querySelector("table");
            const histBody = histTable.tBodies[0];
            observations.histHeaders = [...histTable.tHead.rows[0].cells].map(th => th.textContent);
            observations.histLabels0 = [...histBody.rows[0].cells].map(td => td.dataset.label);
            observations.histLabels1 = [...histBody.rows[1].cells].map(td => td.dataset.label);
            observations.histDisplay = getComputedStyle(histBody.rows[0].cells[0]).display;
            observations.histRowDisplay = getComputedStyle(histBody.rows[0]).display;
            observations.histAllHaveLabel = [...histBody.querySelectorAll("td")]
              .every(td => typeof td.dataset.label === "string" && td.dataset.label.length > 0);
        """
        data = self.observe_page(exercise, 390)
        self.assertEqual(data["histHeaders"], ["시각", "단계", "메모", "다음 할 일"])
        self.assertEqual(data["histLabels0"], data["histHeaders"])
        self.assertEqual(data["histLabels1"], data["histHeaders"])
        self.assertTrue(data["histAllHaveLabel"])
        # history mini-cards: stacked block cells.
        self.assertEqual(data["histDisplay"], "block")

    def test_intake_stage_badge_renders_with_intake_class(self):
        data = self.observe_page("""
            snapshot.tasks = [...snapshot.tasks,
              {...task("intake", "Engineering", "진행 중"), stage: "접수", title: "막 받은 요청"}];
            await tick();
            const toggle = [...document.querySelectorAll(".task-toggle")]
              .find(node => node.dataset.taskId === "intake");
            const badge = toggle.closest(".task-row").querySelector(".badge");
            const style = getComputedStyle(badge);
            observations.intakeBadge = [badge.className, badge.textContent,
              style.backgroundColor, style.color];
            observations.intakeIsIntake = badge.classList.contains("intake");
            const activeBadge = [...document.querySelectorAll(".task-toggle")]
              .find(node => node.dataset.taskId === "active")
              .closest(".task-row").querySelector(".badge");
            observations.activeBadgeClass = activeBadge.className;
        """)
        self.assertEqual(data["intakeBadge"][0], "badge intake")
        self.assertEqual(data["intakeBadge"][1], "접수")
        self.assertEqual(data["intakeBadge"][2], "rgb(236, 232, 251)")
        self.assertEqual(data["intakeBadge"][3], "rgb(74, 58, 153)")
        self.assertTrue(data["intakeIsIntake"])
        self.assertNotIn("intake", data["activeBadgeClass"])

    def test_mobile_columns_backdrop_toggles_with_panel(self):
        exercise = """
            const button = document.getElementById("columns-button");
            const panel = document.getElementById("columns-panel");
            const backdrop = document.getElementById("columns-backdrop");
            observations.exists = [backdrop !== null, backdrop && backdrop.id,
              backdrop && backdrop.className, backdrop && backdrop.tagName];
            observations.is640 = matchMedia("(max-width: 640px)").matches;
            observations.initial = [panel.hidden, backdrop.hidden, button.getAttribute("aria-expanded")];
            button.click();
            observations.opened = [panel.hidden, backdrop.hidden, button.getAttribute("aria-expanded")];
            observations.openDisplay = getComputedStyle(backdrop).display;
            observations.sheetPosition = getComputedStyle(panel).position;
            backdrop.click();
            observations.afterBackdropClick = [panel.hidden, backdrop.hidden, button.getAttribute("aria-expanded")];
            button.click();
            observations.reopened = [panel.hidden, backdrop.hidden];
            document.querySelector("h1").click();
            observations.afterOutside = [panel.hidden, backdrop.hidden];
            button.click();
            const first = panel.querySelector("input");
            first.focus();
            first.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true, cancelable: true}));
            observations.afterEscape = [panel.hidden, backdrop.hidden, document.activeElement.id];
        """
        data = self.observe_page(exercise, 390)
        self.assertEqual(data["exists"], [True, "columns-backdrop", "columns-backdrop", "DIV"])
        self.assertTrue(data["is640"])
        self.assertEqual(data["initial"], [True, True, "false"])
        self.assertEqual(data["opened"], [False, False, "true"])
        # On mobile the backdrop is a visible fixed overlay.
        self.assertNotEqual(data["openDisplay"], "none")
        self.assertEqual(data["sheetPosition"], "fixed")
        self.assertEqual(data["afterBackdropClick"], [True, True, "false"])
        self.assertEqual(data["reopened"], [False, False])
        self.assertEqual(data["afterOutside"], [True, True])
        self.assertEqual(data["afterEscape"], [True, True, "columns-button"])

    def test_completed_always_last_after_render(self):
        exercise = """
            const t = (id, stage, updated) => ({task_id: id, bot: "Engineering", title: id, stage,
              status: stage === "완료" ? "완료" : stage === "막힘" ? "막힘" : "진행 중",
              started: stamp, updated, events: []});
            snapshot.tasks = [
              t("done-new", "완료", "2026-11-18T16:30:00+09:00"),
              t("active-old", "코딩", "2026-11-18T09:00:00+09:00"),
              t("done-old", "완료", "2026-11-18T08:00:00+09:00"),
              t("active-new", "코딩", "2026-11-18T15:00:00+09:00"),
              t("blocked", "막힘", "2026-11-18T14:00:00+09:00"),
            ];
            await tick();
            observations.domOrder = [...document.querySelectorAll(".task-toggle")]
              .map(node => node.dataset.taskId);
            observations.hasHelper = [typeof window.sortTasks === "function", typeof sortTasks === "function"];
            const mixed = [
              t("done-new", "완료", "2026-11-18T16:30:00+09:00"),
              t("active-old", "코딩", "2026-11-18T09:00:00+09:00"),
              t("done-old", "완료", "2026-11-18T08:00:00+09:00"),
              t("active-new", "코딩", "2026-11-18T15:00:00+09:00"),
              t("blocked", "막힘", "2026-11-18T14:00:00+09:00"),
            ];
            observations.defaultOrder = sortTasks(mixed).map(task => task.task_id);
            observations.ascending = sortTasks(mixed,
              (a, b) => String(a.updated).localeCompare(String(b.updated))).map(task => task.task_id);
            observations.descending = sortTasks(mixed,
              (a, b) => String(b.updated).localeCompare(String(a.updated))).map(task => task.task_id);
            observations.reversedInput = sortTasks([...mixed].reverse()).map(task => task.task_id);
            observations.inputKept = mixed.map(task => task.task_id);
            const active = t("only-active", "코딩", "2026-11-18T08:00:00+09:00");
            const statusDone = {task_id: "status-done", bot: "Engineering", title: "s",
              stage: "코딩", status: "완료", started: stamp,
              updated: "2026-11-18T16:30:00+09:00", events: []};
            const stageDone = {task_id: "stage-done", bot: "Engineering", title: "s",
              stage: "완료", status: "진행 중", started: stamp,
              updated: "2026-11-18T16:30:00+09:00", events: []};
            observations.statusLast = sortTasks([statusDone, active]).map(task => task.task_id);
            observations.stageLast = sortTasks([stageDone, active]).map(task => task.task_id);
        """
        # 기본 순서: 진행 중(task_id) → 막힘 → 완료(task_id).
        # 커스텀 비교기 뒤에서도 상태 그룹 파티션이 적용된다.
        expected_default = ["active-new", "active-old", "blocked", "done-new", "done-old"]
        expected_asc = ["active-old", "active-new", "blocked", "done-old", "done-new"]
        for width in (1280, 390):
            with self.subTest(width=width):
                data = self.observe_page(exercise, width)
                self.assertEqual(data["hasHelper"], [True, True])
                self.assertEqual(data["domOrder"], expected_default)
                self.assertEqual(data["defaultOrder"], expected_default)
                self.assertEqual(data["ascending"], expected_asc)
                self.assertEqual(data["descending"], expected_default)
                self.assertEqual(data["reversedInput"], expected_default)
                self.assertEqual(
                    data["inputKept"],
                    ["done-new", "active-old", "done-old", "active-new", "blocked"],
                )
                self.assertEqual(data["statusLast"], ["only-active", "status-done"])
                self.assertEqual(data["stageLast"], ["only-active", "stage-done"])

    def test_default_order_dom_at_both_widths(self):
        # 렌더된 DOM 순서도 상태 그룹(진행 중 → 막힘 → 완료)과
        # 그룹 안 task_id 자연 정렬을 따른다. 업데이트 순이 아니다.
        exercise = """
            const t = (id, stage, updated) => ({task_id: id, bot: "Engineering", title: id, stage,
              status: stage === "완료" ? "완료" : stage === "막힘" ? "막힘" : "진행 중",
              started: stamp, updated, events: []});
            snapshot.tasks = [
              t("task-10", "코딩", "2026-11-18T08:00:00+09:00"),
              t("blocked-z", "막힘", "2026-11-18T16:30:00+09:00"),
              t("a", "코딩", "2026-11-18T16:00:00+09:00"),
              t("done-2", "완료", "2026-11-18T16:28:00+09:00"),
              t("task-2", "코딩", "2026-11-18T15:00:00+09:00"),
              t("blocked-a", "막힘", "2026-11-18T07:30:00+09:00"),
              t("B", "코딩", "2026-11-18T07:00:00+09:00"),
              t("Task-1", "코딩", "2026-11-18T12:00:00+09:00"),
              t("done-10", "완료", "2026-11-18T16:29:00+09:00"),
            ];
            await tick();
            observations.domOrder = [...document.querySelectorAll(".task-toggle")]
              .map(node => node.dataset.taskId);
        """
        expected = ["a", "B", "Task-1", "task-2", "task-10", "blocked-a", "blocked-z", "done-2", "done-10"]
        for width in (1280, 390):
            with self.subTest(width=width):
                data = self.observe_page(exercise, width)
                self.assertEqual(data["domOrder"], expected)

    def test_sort_tasks_status_derives_from_stage_when_missing(self):
        # status가 없으면 stage에서 유도한다(접수..점검은 진행 중).
        # status가 있으면 stage보다 우선한다.
        exercise = """
            const t = (id, stage, status) => ({task_id: id, bot: "Engineering", title: id,
              stage, ...(status === undefined ? {} : {status}),
              started: stamp, updated: stamp, events: []});
            observations.derived = sortTasks([
              t("z-done", "완료"), t("a-blocked", "막힘"), t("m-active", "코딩"),
              t("b-intake", "접수"), t("c-status-blocked", "코딩", "막힘"),
              t("d-status-done", "코딩", "완료"),
            ]).map(task => task.task_id);
        """
        data = self.observe_page(exercise, 1280)
        # 진행 중(b-intake, m-active) → 막힘(a-blocked, c-status-blocked) → 완료(d-status-done, z-done)
        self.assertEqual(data["derived"],
                         ["b-intake", "m-active", "a-blocked", "c-status-blocked", "d-status-done", "z-done"])

    def test_sort_tasks_natural_numeric_compare(self):
        # task_id 숫자는 수치로 비교한다(task-2가 task-10보다 앞).
        exercise = """
            const t = (id, status) => ({task_id: id, bot: "Engineering", title: id,
              stage: status === "완료" ? "완료" : status === "막힘" ? "막힘" : "코딩",
              status, started: stamp, updated: stamp, events: []});
            const ids = ["task-2", "task-10", "Task-1", "a", "B"];
            // 업데이트를 역순으로 주어 업데이트가 앞세우지 못하게 한다.
            const tasks = ids.map((id, i) => ({...t(id, "진행 중"),
              updated: "2026-11-18T" + String(12 - i).padStart(2, "0") + ":00:00+09:00"}));
            observations.natural = sortTasks(tasks).map(task => task.task_id);
            observations.numericPair = sortTasks([t("task-10", "진행 중"), t("task-2", "진행 중")])
              .map(task => task.task_id);
            observations.grouped = sortTasks([
              t("task-10", "완료"), t("task-2", "진행 중"), t("a", "막힘"), t("B", "진행 중"),
            ]).map(task => task.task_id);
        """
        data = self.observe_page(exercise, 1280)
        self.assertEqual(data["natural"], ["a", "B", "Task-1", "task-2", "task-10"])
        self.assertEqual(data["numericPair"], ["task-2", "task-10"])
        # 진행 중(B, task-2) → 막힘(a) → 완료(task-10)
        self.assertEqual(data["grouped"], ["B", "task-2", "a", "task-10"])

    def test_sort_tasks_tiebreak_updated_desc_for_equal_ids(self):
        # task_id 비교가 동률이면 마지막 업데이트 내림차순이다.
        # load_tasks는 ID로 묶으므로 중복 ID는 직접 sortTasks 호출로만 관찰한다.
        exercise = """
            const t = (id, updated) => ({task_id: id, bot: "Engineering", title: id, stage: "코딩",
              status: "진행 중", started: stamp, updated, events: []});
            const dup = [
              t("dup", "2026-11-18T08:00:00+09:00"),
              t("dup", "2026-11-18T16:00:00+09:00"),
              t("dup", "2026-11-18T12:00:00+09:00"),
            ];
            observations.dupOrder = sortTasks(dup).map(task => task.updated);
            const cased = [
              t("Task-1", "2026-11-18T08:00:00+09:00"),
              t("task-1", "2026-11-18T16:00:00+09:00"),
            ];
            observations.casedOrder = sortTasks(cased).map(task => task.task_id);
        """
        data = self.observe_page(exercise, 1280)
        self.assertEqual(data["dupOrder"], [
            "2026-11-18T16:00:00+09:00",
            "2026-11-18T12:00:00+09:00",
            "2026-11-18T08:00:00+09:00",
        ])
        self.assertEqual(data["casedOrder"], ["task-1", "Task-1"])

    def test_sort_tasks_custom_comparator_still_partitions(self):
        # 커스텀 비교기 뒤에도 상태 파티션(진행 중, 막힘, 완료)이 적용된다.
        exercise = """
            const t = (id, stage, updated) => ({task_id: id, bot: "Engineering", title: id, stage,
              status: stage === "완료" ? "완료" : stage === "막힘" ? "막힘" : "진행 중",
              started: stamp, updated, events: []});
            const mixed = [
              t("done-new", "완료", "2026-11-18T16:30:00+09:00"),
              t("active-old", "코딩", "2026-11-18T09:00:00+09:00"),
              t("done-old", "완료", "2026-11-18T08:00:00+09:00"),
              t("active-new", "코딩", "2026-11-18T15:00:00+09:00"),
              t("blocked", "막힘", "2026-11-18T14:00:00+09:00"),
            ];
            observations.byUpdatedDesc = sortTasks(mixed,
              (a, b) => String(b.updated).localeCompare(String(a.updated))).map(task => task.task_id);
            observations.byUpdatedAsc = sortTasks(mixed,
              (a, b) => String(a.updated).localeCompare(String(b.updated))).map(task => task.task_id);
            observations.byIdDesc = sortTasks(mixed,
              (a, b) => String(b.task_id).localeCompare(String(a.task_id))).map(task => task.task_id);
        """
        data = self.observe_page(exercise, 1280)
        self.assertEqual(data["byUpdatedDesc"],
                         ["active-new", "active-old", "blocked", "done-new", "done-old"])
        self.assertEqual(data["byUpdatedAsc"],
                         ["active-old", "active-new", "blocked", "done-old", "done-new"])
        self.assertEqual(data["byIdDesc"],
                         ["active-old", "active-new", "blocked", "done-old", "done-new"])
        for order in (data["byUpdatedDesc"], data["byUpdatedAsc"], data["byIdDesc"]):
            self.assertEqual(order[-2:], [order[-2], order[-1]])
            self.assertLess(order.index("blocked"), order.index("done-new"))
            self.assertLess(order.index("done-old"), len(order))
            self.assertIn(order[0], ("active-new", "active-old"))
            self.assertNotIn("done-new", order[:2])
            self.assertNotIn("done-old", order[:2])


    def test_search_matching_and_helpers(self):
        extra = """
        snapshot.tasks = [
          {task_id: "and-task", bot: "Engineering", title: "Alpha Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T12:00:00+09:00", note: "latest note", next: "latest next",
           events: [
             {task_id: "and-task", bot: "Engineering", title: "Alpha Title", stage: "코딩", note: "beta note here", next: "first next", ts: "2026-11-18T09:00:00+09:00"},
             {task_id: "and-task", bot: "Engineering", title: "Alpha Title", stage: "코딩", note: "latest note", next: "latest next", ts: "2026-11-18T12:00:00+09:00"}
           ]},
          {task_id: "split-task", bot: "Engineering", title: "Split Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T11:00:00+09:00", note: "latest", next: "latest",
           events: [
             {task_id: "split-task", bot: "Engineering", title: "Split Title", stage: "코딩", note: "alpha only", next: "n1", ts: "2026-11-18T09:00:00+09:00"},
             {task_id: "split-task", bot: "Engineering", title: "Split Title", stage: "코딩", note: "beta only", next: "n2", ts: "2026-11-18T11:00:00+09:00"}
           ]},
          {task_id: "old-task", bot: "QA", title: "Old Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T10:00:00+09:00", note: "latest plain", next: "plain next",
           events: [
             {task_id: "old-task", bot: "QA", title: "Old Title", stage: "계획", note: "needle in old event", next: "old next", ts: "2026-11-18T09:00:00+09:00"},
             {task_id: "old-task", bot: "QA", title: "Old Title", stage: "코딩", note: "latest plain", next: "plain next", ts: "2026-11-18T10:00:00+09:00"}
           ]},
          {task_id: "current-only", bot: "Engineering", title: "UniqueCurrent Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T09:30:00+09:00", note: "UniqueCurrent note", next: "UniqueCurrent next",
           events: [
             {task_id: "current-only", bot: "Engineering", title: "Plain Title", stage: "코딩", note: "plain", next: "plain", ts: "2026-11-18T09:30:00+09:00"}
           ]}
        ];
        """
        data = self.observe_page("""
            const waitFor = async (fn, timeout = 3000) => {
              const start = Date.now();
              while (Date.now() - start < timeout) {
                try { if (fn()) return true; } catch (e) {}
                await new Promise(resolve => setTimeout(resolve, 25));
              }
              return false;
            };
            const searchEl = document.getElementById("search");
            const applySearch = (query) => {
              searchEl.value = query;
              searchEl.dispatchEvent(new Event("input", {bubbles: true}));
              searchEl.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
            };
            const ids = () => [...document.querySelectorAll(".task-toggle")].map(node => node.dataset.taskId);
            const counts = () => [...document.querySelectorAll(".count")].map(node => Number(node.textContent));
            const ev = {task_id: "x", title: "Alpha", bot: "Eng", stage: "코딩", note: "beta", next: "", ts: "2026-11-18T09:00:00+09:00"};
            observations.helpers = {
              types: [typeof window.searchKeywordsFor, typeof window.searchEventMatches, typeof window.matchingEventsForTask, typeof window.normalizeSearchText, typeof window.appendHighlightedText],
              keywords: searchKeywordsFor("  Alpha  beta  "),
              empty: searchKeywordsFor("   "),
              normalized: normalizeSearchText("AbC"),
              matchBoth: searchEventMatches(ev, ["alpha", "beta"]),
              matchMissing: searchEventMatches(ev, ["alpha", "gamma"]),
              matchEmpty: searchEventMatches(ev, []),
              matchedCount: matchingEventsForTask(snapshot.tasks[0], ["alpha"]).length,
              matchedTs: searchEventMatches(ev, ["09:00"]),
            };
            observations.initialIds = ids();
            applySearch("ALPHA");
            observations.caseIds = ids();
            observations.caseMarks = [...document.querySelectorAll(".task-row mark")].map(node => node.textContent);
            applySearch("alpha beta");
            observations.andIds = ids();
            observations.andCounts = counts();
            observations.andStatus = document.getElementById("search-status").textContent;
            applySearch("09:00");
            observations.tsIds = ids();
            applySearch("needle");
            observations.needleIds = ids();
            const hist = document.querySelector(".history");
            observations.needleHeading = hist ? hist.querySelector("h2").textContent : null;
            observations.needleCount = hist ? hist.querySelector("table").tBodies[0].rows.length : -1;
            observations.needleNotes = hist ? [...hist.querySelector("table").tBodies[0].rows].map(tr => tr.cells[2].textContent) : [];
            observations.needleColSpan = hist ? hist.cells[0].colSpan : -1;
            observations.needleHit = hist ? [...hist.querySelector("table").tBodies[0].rows].map(tr => tr.classList.contains("hit")) : [];
            applySearch("UniqueCurrent");
            observations.currentIds = ids();
            observations.currentCounts = counts();
            observations.emptyText = document.querySelector("td.empty") ? document.querySelector("td.empty").textContent : null;
            observations.emptySpan = document.querySelector("td.empty") ? document.querySelector("td.empty").colSpan : -1;
            observations.noResultStatus = document.getElementById("search-status").textContent;
            applySearch("");
            observations.clearedIds = ids();
            observations.clearedStatusHidden = document.getElementById("search-status").hidden;
            observations.clearedHasHistory = document.querySelector(".history") !== null;
        """, 1280, extra_setup=extra)
        self.assertEqual(data["helpers"]["types"], ["function"] * 5)
        self.assertEqual(data["helpers"]["keywords"], ["alpha", "beta"])
        self.assertEqual(data["helpers"]["empty"], [])
        self.assertEqual(data["helpers"]["normalized"], "abc")
        self.assertTrue(data["helpers"]["matchBoth"])
        self.assertFalse(data["helpers"]["matchMissing"])
        self.assertFalse(data["helpers"]["matchEmpty"])
        self.assertEqual(data["helpers"]["matchedCount"], 2)
        self.assertTrue(data["helpers"]["matchedTs"])
        self.assertEqual(data["initialIds"], ["and-task", "current-only", "old-task", "split-task"])
        self.assertEqual(data["caseIds"], ["and-task", "split-task"])
        self.assertIn("Alpha", data["caseMarks"])
        self.assertEqual(data["andIds"], ["and-task"])
        self.assertEqual(data["andCounts"], [1, 1, 0, 0])
        self.assertEqual(data["andStatus"], '검색어 "alpha beta": 1개 작업, 1개 이벤트 일치')
        self.assertEqual(data["tsIds"], ["and-task", "old-task", "split-task"])
        self.assertEqual(data["needleIds"], ["old-task"])
        self.assertEqual(data["needleHeading"], "일치한 이벤트 1개 (전체 2개)")
        self.assertEqual(data["needleCount"], 1)
        self.assertEqual(data["needleNotes"], ["needle in old event"])
        self.assertEqual(data["needleColSpan"], 7)
        self.assertEqual(data["needleHit"], [True])
        self.assertEqual(data["currentIds"], [])
        self.assertEqual(data["currentCounts"], [0, 0, 0, 0])
        self.assertEqual(data["emptyText"], "일치하는 이벤트가 없습니다.")
        self.assertEqual(data["emptySpan"], 7)
        self.assertEqual(data["noResultStatus"], '검색어 "UniqueCurrent": 0개 작업, 0개 이벤트 일치')
        self.assertEqual(data["clearedIds"], data["initialIds"])
        self.assertTrue(data["clearedStatusHidden"])
        self.assertFalse(data["clearedHasHistory"])


    def test_search_highlight_safe_and_full_history(self):
        extra = """
        snapshot.tasks = [
          {task_id: "safe-task", bot: "Engineering", title: "Safe Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T12:00:00+09:00", note: "x <b>x</b> a.c( y", next: "next <i>y</i>",
           events: [
             {task_id: "safe-task", bot: "Engineering", title: "Safe Title", stage: "코딩", note: "plain other", next: "plain next", ts: "2026-11-18T09:00:00+09:00"},
             {task_id: "safe-task", bot: "Engineering", title: "Safe Title", stage: "코딩", note: "x <b>x</b> a.c( y", next: "next <i>y</i>", ts: "2026-11-18T12:00:00+09:00"}
           ]},
          {task_id: "abc-task", bot: "Engineering", title: "Abc Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T11:00:00+09:00", note: "abc", next: "abc next",
           events: [
             {task_id: "abc-task", bot: "Engineering", title: "Abc Title", stage: "코딩", note: "abc", next: "abc next", ts: "2026-11-18T11:00:00+09:00"}
           ]},
          {task_id: "overlap-task", bot: "Engineering", title: "Overlap", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T10:00:00+09:00", note: "hello", next: "hello next",
           events: [
             {task_id: "overlap-task", bot: "Engineering", title: "Overlap", stage: "코딩", note: "hello", next: "hello next", ts: "2026-11-18T10:00:00+09:00"}
           ]}
        ];
        """
        data = self.observe_page("""
            const searchEl = document.getElementById("search");
            const applySearch = (query) => {
              searchEl.value = query;
              searchEl.dispatchEvent(new Event("input", {bubbles: true}));
              searchEl.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
            };
            const ids = () => [...document.querySelectorAll(".task-toggle")].map(node => node.dataset.taskId);
            const rowById = id => [...document.querySelectorAll(".task-row")].find(row => row.querySelector(".task-toggle")?.dataset.taskId === id);
            const noteCell = id => rowById(id)?.querySelector('td[data-column="note"]');
            applySearch("a.c(");
            observations.safeIds = ids();
            observations.safeMarks = [...document.querySelectorAll(".task-row mark")].map(node => node.textContent);
            observations.safeNoteText = noteCell("safe-task")?.textContent;
            observations.safeInjectedB = document.querySelectorAll("#rows b").length;
            const safeHist = document.querySelector(".history");
            observations.safeMatchHeading = safeHist ? safeHist.querySelector("h2").textContent : null;
            observations.safeMatchCount = safeHist ? safeHist.querySelector("table").tBodies[0].rows.length : -1;
            observations.safeHistoryB = safeHist ? safeHist.querySelectorAll("b").length : -1;
            observations.safeMarkStyle = (() => {
              const mark = document.querySelector(".task-row mark");
              if (!mark) return null;
              const style = getComputedStyle(mark);
              return [style.backgroundColor, style.color, style.fontWeight];
            })();
            const idMarkWeight = (() => {
              const idSpan = document.querySelector(".task-row .task-id");
              const mark = idSpan ? idSpan.querySelector("mark") : null;
              if (!idSpan) return null;
              return [getComputedStyle(idSpan).fontWeight, mark ? getComputedStyle(mark).fontWeight : null];
            })();
            observations.idMarkWeight = idMarkWeight;
            const toggle = [...document.querySelectorAll(".task-toggle")].find(node => node.dataset.taskId === "safe-task");
            toggle.focus();
            toggle.click();
            const full = document.querySelector(".history");
            observations.fullHeading = full ? full.querySelector("h2").textContent : null;
            observations.fullCount = full ? full.querySelector("table").tBodies[0].rows.length : -1;
            observations.fullHits = full ? [...full.querySelector("table").tBodies[0].rows].map(tr => tr.classList.contains("hit")) : [];
            observations.fullMarkCounts = full ? [...full.querySelector("table").tBodies[0].rows].map(tr => tr.querySelectorAll("mark").length) : [];
            observations.fullLabels = full ? [...full.querySelector("table").tBodies[0].rows[0].cells].map(td => td.dataset.label) : [];
            observations.fullColSpan = full ? full.cells[0].colSpan : -1;
            applySearch("a.c");
            observations.dotIds = ids();
            applySearch("hell ello");
            observations.overlapIds = ids();
            const overlapNote = noteCell("overlap-task");
            observations.overlapMarks = overlapNote ? [...overlapNote.querySelectorAll("mark")].map(node => node.textContent) : [];
            observations.overlapText = overlapNote ? overlapNote.textContent : null;
            observations.overlapNested = overlapNote ? overlapNote.querySelectorAll("mark mark").length : -1;
            applySearch("safe");
            const safeIdSpan = document.querySelector(".task-row .task-id");
            const safeIdMark = safeIdSpan ? safeIdSpan.querySelector("mark") : null;
            observations.safeIdMark = safeIdMark ? [safeIdMark.textContent, getComputedStyle(safeIdMark).fontWeight, getComputedStyle(safeIdSpan).fontWeight] : null;
        """, 1280, extra_setup=extra)
        self.assertEqual(data["safeIds"], ["safe-task"])
        self.assertIn("a.c(", data["safeMarks"])
        self.assertEqual(data["safeNoteText"], "x <b>x</b> a.c( y")
        self.assertEqual(data["safeInjectedB"], 0)
        self.assertEqual(data["safeMatchHeading"], "일치한 이벤트 1개 (전체 2개)")
        self.assertEqual(data["safeMatchCount"], 1)
        self.assertEqual(data["safeHistoryB"], 0)
        self.assertEqual(data["safeMarkStyle"][0], "rgb(255, 241, 168)")
        self.assertEqual(data["idMarkWeight"][0], "700")
        # task-id has no safe keyword in this fixture, so mark weight is null; still bold check holds.
        self.assertEqual(data["fullHeading"], "이벤트 이력")
        self.assertEqual(data["fullCount"], 2)
        self.assertEqual(data["fullHits"], [False, True])
        self.assertEqual(data["fullLabels"], ["시각", "단계", "메모", "다음 할 일"])
        self.assertEqual(data["fullColSpan"], 7)
        self.assertEqual(data["fullMarkCounts"][1], 1)
        self.assertEqual(data["fullMarkCounts"][0], 0)
        self.assertEqual(data["dotIds"], ["safe-task"])
        self.assertEqual(data["overlapIds"], ["overlap-task"])
        self.assertEqual(data["overlapMarks"], ["hello"])
        self.assertEqual(data["overlapText"], "hello")
        self.assertEqual(data["overlapNested"], 0)
        self.assertEqual(data["safeIdMark"], ["safe", "700", "700"])


    def test_search_counts_bot_status_and_columns(self):
        extra = """
        const needleEvent = (taskId, bot, ts) => ({task_id: taskId, bot, title: taskId + " Title", stage: "코딩",
          note: "needle note", next: "needle next", ts});
        snapshot.tasks = [
          {task_id: "eng-active", bot: "Engineering", title: "Eng Active", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T12:00:00+09:00", note: "needle note", next: "needle next",
           events: [needleEvent("eng-active", "Engineering", "2026-11-18T12:00:00+09:00")]},
          {task_id: "eng-done", bot: "Engineering", title: "Eng Done", stage: "완료", status: "완료",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T11:00:00+09:00", note: "needle note", next: "needle next",
           events: [needleEvent("eng-done", "Engineering", "2026-11-18T11:00:00+09:00")]},
          {task_id: "qa-active", bot: "QA", title: "Qa Active", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T10:00:00+09:00", note: "needle note", next: "needle next",
           events: [needleEvent("qa-active", "QA", "2026-11-18T10:00:00+09:00")]},
          {task_id: "eng-plain", bot: "Engineering", title: "Eng Plain", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T09:00:00+09:00", note: "plain", next: "plain",
           events: [{task_id: "eng-plain", bot: "Engineering", title: "Eng Plain", stage: "코딩", note: "plain", next: "plain", ts: "2026-11-18T09:00:00+09:00"}]},
          {task_id: "qa-blocked", bot: "QA", title: "Qa Blocked", stage: "막힘", status: "막힘",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T08:00:00+09:00", note: "needle note", next: "needle next",
           events: [needleEvent("qa-blocked", "QA", "2026-11-18T08:00:00+09:00")]}
        ];
        """
        data = self.observe_page("""
            const waitFor = async (fn, timeout = 3000) => {
              const start = Date.now();
              while (Date.now() - start < timeout) {
                try { if (fn()) return true; } catch (e) {}
                await new Promise(resolve => setTimeout(resolve, 25));
              }
              return false;
            };
            const searchEl = document.getElementById("search");
            const applySearch = (query) => {
              searchEl.value = query;
              searchEl.dispatchEvent(new Event("input", {bubbles: true}));
              searchEl.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
            };
            const ids = () => [...document.querySelectorAll(".task-toggle")].map(node => node.dataset.taskId);
            const counts = () => [...document.querySelectorAll(".count")].map(node => Number(node.textContent));
            const select = document.getElementById("bot-filter");
            applySearch("needle");
            observations.allIds = ids();
            observations.allCounts = counts();
            observations.allStatus = document.getElementById("search-status").textContent;
            select.value = "name:Engineering";
            select.dispatchEvent(new Event("change"));
            observations.engIds = ids();
            observations.engCounts = counts();
            observations.engStatus = document.getElementById("search-status").textContent;
            document.querySelector('[data-status="완료"]').click();
            observations.doneIds = ids();
            observations.doneCounts = counts();
            observations.doneStatus = document.getElementById("search-status").textContent;
            const table = document.querySelector(".tasks");
            const panel = document.getElementById("columns-panel");
            const button = document.getElementById("columns-button");
            const byKey = key => panel.querySelector(`input[data-column="${key}"]`);
            button.click();
            byKey("stage").click();
            byKey("note").click();
            observations.colHeaders = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.colRowCells = table.tBodies[0].rows[0].cells.length;
            const hist = table.querySelector(".history");
            observations.colHistSpan = hist ? hist.cells[0].colSpan : -1;
            observations.colHistHeading = hist ? hist.querySelector("h2").textContent : null;
            observations.colIds = ids();
            panel.querySelector('.column-item[data-column="bot"] button[data-direction="down"]').click();
            panel.querySelector('.column-item[data-column="bot"] button[data-direction="down"]').click();
            observations.movedHeaders = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.movedRowCells = [...table.tBodies[0].rows[0].cells].map(node => node.textContent);
            observations.movedHistSpan = table.querySelector(".history") ? table.querySelector(".history").cells[0].colSpan : -1;
            observations.movedIds = ids();
            applySearch("no-such-needle");
            observations.emptyHeaders = [...table.tHead.rows[0].cells].map(node => node.textContent);
            observations.emptySpan = document.querySelector("td.empty") ? document.querySelector("td.empty").colSpan : -1;
            observations.emptyText = document.querySelector("td.empty") ? document.querySelector("td.empty").textContent : null;
            observations.emptyStatus = document.getElementById("search-status").textContent;
        """, 1280, extra_setup=extra)
        self.assertEqual(data["allIds"], ["eng-active", "qa-active", "qa-blocked", "eng-done"])
        self.assertEqual(data["allCounts"], [4, 2, 1, 1])
        self.assertEqual(data["allStatus"], '검색어 "needle": 4개 작업, 4개 이벤트 일치')
        self.assertEqual(data["engIds"], ["eng-active", "eng-done"])
        self.assertEqual(data["engCounts"], [2, 1, 1, 0])
        self.assertEqual(data["engStatus"], '검색어 "needle": 2개 작업, 2개 이벤트 일치')
        self.assertEqual(data["doneIds"], ["eng-done"])
        self.assertEqual(data["doneCounts"], [2, 1, 1, 0])
        self.assertEqual(data["doneStatus"], '검색어 "needle": 1개 작업, 1개 이벤트 일치')
        self.assertEqual(data["colHeaders"], ["봇", "작업", "시작", "마지막 업데이트", "다음 할 일"])
        self.assertEqual(data["colRowCells"], 5)
        self.assertEqual(data["colHistSpan"], 5)
        self.assertEqual(data["colHistHeading"], "일치한 이벤트 1개 (전체 1개)")
        self.assertEqual(data["colIds"], ["eng-done"])
        self.assertEqual(data["movedHeaders"][0], "작업")
        self.assertEqual(data["movedHeaders"][1], "봇")
        self.assertEqual(data["movedRowCells"][0], "eng-doneEng Done")
        self.assertEqual(data["movedRowCells"][1], "Engineering")
        self.assertEqual(data["movedHistSpan"], 5)
        self.assertEqual(data["movedIds"], ["eng-done"])
        self.assertEqual(data["emptyHeaders"], data["movedHeaders"])
        self.assertEqual(data["emptySpan"], 5)
        self.assertEqual(data["emptyText"], "일치하는 이벤트가 없습니다.")
        self.assertEqual(data["emptyStatus"], '검색어 "no-such-needle": 0개 작업, 0개 이벤트 일치')


    def test_search_url_preload_and_popstate(self):
        extra = """
        try { history.replaceState(null, "", "?q=needle&foo=bar"); } catch (e) { window.__preloadError = String(e); }
        snapshot.tasks = [
          {task_id: "need-task", bot: "Engineering", title: "Needle Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T12:00:00+09:00", note: "needle note", next: "needle next",
           events: [{task_id: "need-task", bot: "Engineering", title: "Needle Title", stage: "코딩", note: "needle note", next: "needle next", ts: "2026-11-18T12:00:00+09:00"}]},
          {task_id: "alpha-task", bot: "Engineering", title: "Alpha Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T11:00:00+09:00", note: "alpha note", next: "alpha next",
           events: [{task_id: "alpha-task", bot: "Engineering", title: "Alpha Title", stage: "코딩", note: "alpha note", next: "alpha next", ts: "2026-11-18T11:00:00+09:00"}]},
          {task_id: "plain-task", bot: "Engineering", title: "Plain", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T10:00:00+09:00", note: "plain", next: "plain",
           events: [{task_id: "plain-task", bot: "Engineering", title: "Plain", stage: "코딩", note: "plain", next: "plain", ts: "2026-11-18T10:00:00+09:00"}]}
        ];
        """
        data = self.observe_page("""
            const searchEl = document.getElementById("search");
            const searchClear = document.getElementById("search-clear");
            const ids = () => [...document.querySelectorAll(".task-toggle")].map(node => node.dataset.taskId);
            const params = new URLSearchParams(location.search);
            observations.initial = {
              value: searchEl.value,
              clearHidden: searchClear.hidden,
              ids: ids(),
              status: document.getElementById("search-status").textContent,
              search: location.search,
              q: params.get("q"),
              foo: params.get("foo"),
            };
            observations.preloadError = window.__preloadError || null;
            history.pushState(null, "", "?q=alpha&foo=bar");
            window.dispatchEvent(new PopStateEvent("popstate"));
            observations.afterPop = {
              value: searchEl.value,
              clearHidden: searchClear.hidden,
              ids: ids(),
              status: document.getElementById("search-status").textContent,
              search: location.search,
            };
            searchEl.value = "needle";
            searchEl.dispatchEvent(new Event("input", {bubbles: true}));
            searchEl.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
            observations.afterType = {
              ids: ids(),
              search: location.search,
              q: new URLSearchParams(location.search).get("q"),
              foo: new URLSearchParams(location.search).get("foo"),
            };
        """, 1280, extra_setup=extra)
        self.assertIsNone(data["preloadError"])
        self.assertEqual(data["initial"]["value"], "needle")
        self.assertFalse(data["initial"]["clearHidden"])
        self.assertEqual(data["initial"]["ids"], ["need-task"])
        self.assertEqual(data["initial"]["status"], '검색어 "needle": 1개 작업, 1개 이벤트 일치')
        self.assertEqual(data["initial"]["q"], "needle")
        self.assertEqual(data["initial"]["foo"], "bar")
        self.assertEqual(data["afterPop"]["value"], "alpha")
        self.assertEqual(data["afterPop"]["ids"], ["alpha-task"])
        self.assertEqual(data["afterPop"]["status"], '검색어 "alpha": 1개 작업, 1개 이벤트 일치')
        self.assertEqual(data["afterType"]["ids"], ["need-task"])
        self.assertEqual(data["afterType"]["q"], "needle")
        self.assertEqual(data["afterType"]["foo"], "bar")


    def test_search_debounce_clear_escape_and_refresh(self):
        extra = """
        snapshot.tasks = [
          {task_id: "need-task", bot: "Engineering", title: "Needle Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T12:00:00+09:00", note: "needle note", next: "needle next",
           events: [{task_id: "need-task", bot: "Engineering", title: "Needle Title", stage: "코딩", note: "needle note", next: "needle next", ts: "2026-11-18T12:00:00+09:00"}]},
          {task_id: "alpha-task", bot: "Engineering", title: "Alpha Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T11:00:00+09:00", note: "alpha note", next: "alpha next",
           events: [{task_id: "alpha-task", bot: "Engineering", title: "Alpha Title", stage: "코딩", note: "alpha note", next: "alpha next", ts: "2026-11-18T11:00:00+09:00"}]},
          {task_id: "plain-task", bot: "Engineering", title: "Plain", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T10:00:00+09:00", note: "plain", next: "plain",
           events: [{task_id: "plain-task", bot: "Engineering", title: "Plain", stage: "코딩", note: "plain", next: "plain", ts: "2026-11-18T10:00:00+09:00"}]}
        ];
        """
        data = self.observe_page("""
            const waitFor = async (fn, timeout = 3000) => {
              const start = Date.now();
              while (Date.now() - start < timeout) {
                try { if (fn()) return true; } catch (e) {}
                await new Promise(resolve => setTimeout(resolve, 25));
              }
              return false;
            };
            const searchEl = document.getElementById("search");
            const searchClear = document.getElementById("search-clear");
            const ids = () => [...document.querySelectorAll(".task-toggle")].map(node => node.dataset.taskId);
            const calls = [];
            const originalReplace = history.replaceState.bind(history);
            history.replaceState = (...args) => { calls.push(args); try { return originalReplace(...args); } catch (e) {} };
            observations.initial = {
              type: searchEl.type, id: searchEl.id, placeholder: searchEl.placeholder,
              autocomplete: searchEl.autocomplete, enterkeyhint: searchEl.getAttribute("enterkeyhint"),
              label: document.querySelector('label[for="search"]') ? document.querySelector('label[for="search"]').textContent : null,
              labelClass: document.querySelector('label[for="search"]') ? document.querySelector('label[for="search"]').className : null,
              clearType: searchClear.type, clearLabel: searchClear.getAttribute("aria-label"),
              clearHidden: searchClear.hidden,
              statusRole: document.getElementById("search-status").getAttribute("role"),
              statusLive: document.getElementById("search-status").getAttribute("aria-live"),
              statusHidden: document.getElementById("search-status").hidden,
              ids: ids(),
            };
            searchEl.value = "needle";
            searchEl.dispatchEvent(new Event("input", {bubbles: true}));
            observations.immediate = {ids: ids(), calls: calls.length, clearHidden: searchClear.hidden};
            observations.debounceOk = await waitFor(() => ids().length === 1 && new URLSearchParams(location.search).get("q") === "needle");
            observations.afterDebounce = {
              ids: ids(),
              calls: calls.length,
              urls: calls.map(args => String(args[2])),
              search: location.search,
              status: document.getElementById("search-status").textContent,
            };
            searchEl.focus();
            snapshot.tasks = [
              {task_id: "need-task", bot: "Engineering", title: "Needle Title", stage: "코딩", status: "진행 중",
               started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T12:00:00+09:00", note: "needle note", next: "needle next",
               events: [{task_id: "need-task", bot: "Engineering", title: "Needle Title", stage: "코딩", note: "needle note", next: "needle next", ts: "2026-11-18T12:00:00+09:00"}]},
              {task_id: "alpha-task", bot: "Engineering", title: "Alpha Title", stage: "코딩", status: "진행 중",
               started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T11:00:00+09:00", note: "alpha note", next: "alpha next",
               events: [{task_id: "alpha-task", bot: "Engineering", title: "Alpha Title", stage: "코딩", note: "alpha note", next: "alpha next", ts: "2026-11-18T11:00:00+09:00"}]}
            ];
            await tick();
            observations.afterRefresh = {
              value: searchEl.value,
              focused: document.activeElement === searchEl,
              ids: ids(),
              search: location.search,
            };
            searchEl.focus();
            searchEl.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true, cancelable: true}));
            observations.afterEscape = {
              value: searchEl.value,
              ids: ids(),
              hasQ: new URLSearchParams(location.search).has("q"),
              clearHidden: searchClear.hidden,
              statusHidden: document.getElementById("search-status").hidden,
            };
            searchEl.value = "needle";
            searchEl.dispatchEvent(new Event("input", {bubbles: true}));
            searchEl.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
            observations.enterImmediate = {ids: ids(), q: new URLSearchParams(location.search).get("q")};
            searchClear.click();
            observations.afterClear = {
              value: searchEl.value,
              ids: ids(),
              hasQ: new URLSearchParams(location.search).has("q"),
              clearHidden: searchClear.hidden,
              statusHidden: document.getElementById("search-status").hidden,
            };
        """, 1280, extra_setup=extra)
        self.assertEqual(data["initial"]["type"], "search")
        self.assertEqual(data["initial"]["id"], "search")
        self.assertEqual(data["initial"]["placeholder"], "이벤트 키워드 검색 (공백으로 여러 단어, AND)")
        self.assertEqual(data["initial"]["autocomplete"], "off")
        self.assertEqual(data["initial"]["enterkeyhint"], "search")
        self.assertEqual(data["initial"]["label"], "이벤트 키워드 검색")
        self.assertEqual(data["initial"]["labelClass"], "visually-hidden")
        self.assertEqual(data["initial"]["clearType"], "button")
        self.assertEqual(data["initial"]["clearLabel"], "검색어 지우기")
        self.assertTrue(data["initial"]["clearHidden"])
        self.assertEqual(data["initial"]["statusRole"], "status")
        self.assertEqual(data["initial"]["statusLive"], "polite")
        self.assertTrue(data["initial"]["statusHidden"])
        self.assertEqual(data["initial"]["ids"], ["alpha-task", "need-task", "plain-task"])
        self.assertEqual(data["immediate"]["ids"], data["initial"]["ids"])
        self.assertFalse(data["immediate"]["clearHidden"])
        self.assertTrue(data["debounceOk"])
        self.assertEqual(data["afterDebounce"]["ids"], ["need-task"])
        self.assertGreaterEqual(data["afterDebounce"]["calls"], 1)
        self.assertTrue(any("q=needle" in url for url in data["afterDebounce"]["urls"]))
        self.assertEqual(data["afterDebounce"]["status"], '검색어 "needle": 1개 작업, 1개 이벤트 일치')
        self.assertEqual(data["afterRefresh"]["value"], "needle")
        self.assertTrue(data["afterRefresh"]["focused"])
        self.assertEqual(data["afterRefresh"]["ids"], ["need-task"])
        self.assertEqual(data["afterRefresh"]["search"], "?q=needle")
        self.assertEqual(data["afterEscape"]["value"], "")
        self.assertEqual(data["afterEscape"]["ids"], ["alpha-task", "need-task"])
        self.assertFalse(data["afterEscape"]["hasQ"])
        self.assertTrue(data["afterEscape"]["clearHidden"])
        self.assertTrue(data["afterEscape"]["statusHidden"])
        self.assertEqual(data["enterImmediate"]["ids"], ["need-task"])
        self.assertEqual(data["enterImmediate"]["q"], "needle")
        self.assertEqual(data["afterClear"]["value"], "")
        self.assertEqual(data["afterClear"]["ids"], ["alpha-task", "need-task"])
        self.assertFalse(data["afterClear"]["hasQ"])
        self.assertTrue(data["afterClear"]["clearHidden"])
        self.assertTrue(data["afterClear"]["statusHidden"])


    def test_search_mobile_layout(self):
        extra = """
        snapshot.tasks = [
          {task_id: "need-task", bot: "Engineering", title: "Needle Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T12:00:00+09:00", note: "needle note", next: "needle next",
           events: [{task_id: "need-task", bot: "Engineering", title: "Needle Title", stage: "코딩", note: "needle note", next: "needle next", ts: "2026-11-18T12:00:00+09:00"}]},
          {task_id: "plain-task", bot: "Engineering", title: "Plain", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T10:00:00+09:00", note: "plain", next: "plain",
           events: [{task_id: "plain-task", bot: "Engineering", title: "Plain", stage: "코딩", note: "plain", next: "plain", ts: "2026-11-18T10:00:00+09:00"}]}
        ];
        """
        exercise = """
            const searchEl = document.getElementById("search");
            const searchClear = document.getElementById("search-clear");
            const applySearch = (query) => {
              searchEl.value = query;
              searchEl.dispatchEvent(new Event("input", {bubbles: true}));
              searchEl.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
            };
            applySearch("needle");
            const inputRect = searchEl.getBoundingClientRect();
            const clearRect = searchClear.getBoundingClientRect();
            const inputStyle = getComputedStyle(searchEl);
            observations.layout = {
              fontSize: parseFloat(inputStyle.fontSize),
              minHeight: parseFloat(inputStyle.minHeight),
              inputHeight: inputRect.height,
              inputWidth: inputRect.width,
              clearWidth: clearRect.width,
              clearHeight: clearRect.height,
              clearHidden: searchClear.hidden,
              noOverflow: [document.documentElement.scrollWidth <= window.innerWidth, document.body.scrollWidth <= window.innerWidth],
              scrollWidth: document.documentElement.scrollWidth,
              innerWidth: window.innerWidth,
              wrapWidth: document.querySelector(".search-wrap").getBoundingClientRect().width,
              filterWidth: document.querySelector(".filter-bar").getBoundingClientRect().width,
              ids: [...document.querySelectorAll(".task-toggle")].map(node => node.dataset.taskId),
              marks: [...document.querySelectorAll("mark")].map(node => node.textContent),
              historyLabels: document.querySelector(".history") ? [...document.querySelectorAll(".history tbody tr")[0].cells].map(td => td.dataset.label) : [],
            };
        """
        for width in (1280, 390):
            with self.subTest(width=width):
                data = self.observe_page(exercise, width, extra_setup=extra)
                layout = data["layout"]
                self.assertGreaterEqual(layout["fontSize"], 16)
                self.assertEqual(layout["ids"], ["need-task"])
                self.assertIn("needle", layout["marks"])
                self.assertEqual(layout["historyLabels"], ["시각", "단계", "메모", "다음 할 일"])
                self.assertFalse(layout["clearHidden"])
                if width == 390:
                    self.assertGreaterEqual(layout["clearWidth"], 44)
                    self.assertGreaterEqual(layout["clearHeight"], 44)
                    self.assertGreaterEqual(layout["inputHeight"], 44)
                    self.assertGreaterEqual(layout["minHeight"], 44)
                    self.assertTrue(all(layout["noOverflow"]))
                    self.assertLessEqual(layout["scrollWidth"], layout["innerWidth"])
                    self.assertGreaterEqual(layout["wrapWidth"], layout["filterWidth"] - 32)


    def test_history_summary_expanded_at_both_widths(self):
        extra = """
        snapshot.tasks = [
          {task_id: "active", bot: "Engineering", title: "기능 구현", stage: "접수", status: "진행 중",
           started: "2026-11-18T10:00:00+09:00", updated: "2026-11-18T12:00:00+09:00",
           note: "구현 중", next: "리뷰 요청",
           events: [
             {task_id: "active", title: "기능 구현", bot: "Engineering", stage: "접수", note: "첫 메모", next: "다음 점검", ts: "2026-11-18T10:00:00+09:00"},
             {task_id: "active", title: "기능 구현", bot: "Engineering", stage: "코딩", note: "구현 중", next: "리뷰 요청", ts: "2026-11-18T12:00:00+09:00"}
           ]},
          {task_id: "plain", bot: "QA", title: "", stage: "완료", status: "완료",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T11:00:00+09:00",
           note: "", next: "",
           events: [
             {task_id: "plain", title: "", bot: "QA", stage: "완료", note: "", next: "", ts: "2026-11-18T11:00:00+09:00"}
           ]}
        ];
        """
        exercise = """
            const table = document.querySelector(".tasks");
            const toggleActive = [...document.querySelectorAll(".task-toggle")].find(node => node.dataset.taskId === "active");
            toggleActive.click();
            const history = table.querySelector(".history");
            const cell = history.cells[0];
            const h2 = cell.querySelector("h2");
            const summary = cell.querySelector(".history-summary");
            const histTable = cell.querySelector("table");
            observations.is640 = matchMedia("(max-width: 640px)").matches;
            observations.order = {
              h2Text: h2.textContent,
              summaryTag: summary ? summary.tagName : null,
              ariaLabel: summary ? summary.getAttribute("aria-label") : null,
              h2NextIsSummary: h2.nextElementSibling === summary,
              summaryNextIsTable: summary ? summary.nextElementSibling === histTable : null,
              children: [...cell.children].map(node => node.tagName + " " + node.className),
            };
            const sectionStyle = summary ? getComputedStyle(summary) : null;
            observations.sectionStyle = sectionStyle ? {
              backgroundColor: sectionStyle.backgroundColor,
              borderTopWidth: sectionStyle.borderTopWidth,
              borderTopStyle: sectionStyle.borderTopStyle,
              borderTopColor: sectionStyle.borderTopColor,
              borderTopLeftRadius: sectionStyle.borderTopLeftRadius,
              padding: [sectionStyle.paddingTop, sectionStyle.paddingRight, sectionStyle.paddingBottom, sectionStyle.paddingLeft],
              marginBottom: sectionStyle.marginBottom,
              marginTop: sectionStyle.marginTop,
              fontSize: sectionStyle.fontSize,
            } : null;
            const idEl = summary ? summary.querySelector(".summary-id") : null;
            observations.idInfo = idEl ? {
              tagName: idEl.tagName, className: idEl.className, textContent: idEl.textContent,
              fontWeight: getComputedStyle(idEl).fontWeight,
              backgroundColor: getComputedStyle(idEl).backgroundColor,
            } : null;
            const titleEl = summary ? summary.querySelector(".summary-title") : null;
            observations.titleInfo = titleEl ? {
              textContent: titleEl.textContent,
              fontWeight: getComputedStyle(titleEl).fontWeight,
            } : null;
            const dl = summary ? summary.querySelector("dl") : null;
            const dlStyle = dl ? getComputedStyle(dl) : null;
            observations.dlInfo = dl ? {
              tagName: dl.tagName, display: dlStyle.display,
              gridTemplateColumns: dlStyle.gridTemplateColumns,
              trackCount: dlStyle.gridTemplateColumns ? dlStyle.gridTemplateColumns.split(" ").filter(Boolean).length : 0,
            } : null;
            observations.dts = dl ? [...dl.querySelectorAll("dt")].map(node => node.textContent) : [];
            observations.dds = dl ? [...dl.querySelectorAll("dd")].map(node => node.textContent) : [];
            const dtEl = dl ? dl.querySelector("dt") : null;
            observations.dtStyle = dtEl ? {
              color: getComputedStyle(dtEl).color,
              fontWeight: getComputedStyle(dtEl).fontWeight,
              fontSize: getComputedStyle(dtEl).fontSize,
            } : null;
            const badgeEl = summary ? summary.querySelector(".badge") : null;
            observations.badgeInfo = badgeEl ? {
              className: badgeEl.className, textContent: badgeEl.textContent,
              backgroundColor: getComputedStyle(badgeEl).backgroundColor,
              color: getComputedStyle(badgeEl).color,
            } : null;
            observations.times = summary ? [...summary.querySelectorAll("time")].map(node => [node.getAttribute("datetime"), node.textContent]) : [];
            const wideDd = dl ? dl.querySelector("dd.summary-wide") : null;
            observations.wideStyle = wideDd ? {
              whiteSpace: getComputedStyle(wideDd).whiteSpace,
              overflowWrap: getComputedStyle(wideDd).overflowWrap,
            } : null;
            observations.histHeaders = [...histTable.tHead.rows[0].cells].map(th => th.textContent);
            observations.noOverflow = [document.documentElement.scrollWidth <= window.innerWidth,
              document.body.scrollWidth <= window.innerWidth];
            const togglePlain = [...document.querySelectorAll(".task-toggle")].find(node => node.dataset.taskId === "plain");
            togglePlain.click();
            const historyPlain = table.querySelector(".history");
            const summaryPlain = historyPlain ? historyPlain.querySelector(".history-summary") : null;
            const dlPlain = summaryPlain ? summaryPlain.querySelector("dl") : null;
            observations.plain = {
              dts: dlPlain ? [...dlPlain.querySelectorAll("dt")].map(node => node.textContent) : [],
              dds: dlPlain ? [...dlPlain.querySelectorAll("dd")].map(node => node.textContent) : [],
              hasTitle: summaryPlain ? summaryPlain.querySelector(".summary-title") !== null : null,
              hasWide: dlPlain ? dlPlain.querySelector("dd.summary-wide") !== null : null,
              heading: historyPlain ? historyPlain.querySelector("h2").textContent : null,
              orderOk: historyPlain ? (historyPlain.querySelector("h2").nextElementSibling === summaryPlain) : null,
            };
            observations.noOverflowAfterPlain = [document.documentElement.scrollWidth <= window.innerWidth,
              document.body.scrollWidth <= window.innerWidth];
        """
        for width in (1280, 390):
            with self.subTest(width=width):
                data = self.observe_page(exercise, width, extra_setup=extra)
                self.assertEqual(data["is640"], width == 390 or (width == 390))
                self.assertEqual(data["order"]["h2Text"], "이벤트 이력")
                self.assertEqual(data["order"]["summaryTag"], "SECTION")
                self.assertEqual(data["order"]["ariaLabel"], "작업 요약")
                self.assertTrue(data["order"]["h2NextIsSummary"])
                self.assertTrue(data["order"]["summaryNextIsTable"])
                style = data["sectionStyle"]
                self.assertEqual(style["backgroundColor"], "rgb(255, 255, 255)")
                self.assertEqual(style["borderTopWidth"], "1px")
                self.assertEqual(style["borderTopStyle"], "solid")
                self.assertEqual(style["borderTopColor"], "rgb(218, 226, 232)")
                self.assertEqual(style["borderTopLeftRadius"], "8px")
                self.assertEqual(style["padding"], ["10px", "12px", "10px", "12px"])
                self.assertEqual(style["marginBottom"], "10px")
                self.assertEqual(style["marginTop"], "0px")
                self.assertAlmostEqual(float(style["fontSize"].replace("px", "")), 13.6, places=1)
                self.assertEqual(data["idInfo"]["tagName"], "STRONG")
                self.assertEqual(data["idInfo"]["className"], "summary-id")
                self.assertEqual(data["idInfo"]["textContent"], "active")
                self.assertEqual(data["idInfo"]["fontWeight"], "700")
                self.assertEqual(data["idInfo"]["backgroundColor"], "rgba(0, 0, 0, 0)")
                self.assertEqual(data["titleInfo"]["textContent"], "기능 구현")
                self.assertEqual(data["titleInfo"]["fontWeight"], "400")
                self.assertEqual(data["dlInfo"]["tagName"], "DL")
                self.assertEqual(data["dlInfo"]["display"], "grid")
                if width == 1280:
                    self.assertEqual(data["dlInfo"]["trackCount"], 4)
                else:
                    self.assertEqual(data["dlInfo"]["trackCount"], 2)
                self.assertEqual(data["dts"], ["봇", "현재 단계", "상태", "시작", "마지막 업데이트", "이벤트 수", "메모", "다음 할 일"])
                self.assertEqual(data["dds"], ["Engineering", "접수", "진행 중", "2026-11-18 10:00", "2026-11-18 12:00", "2개", "구현 중", "리뷰 요청"])
                dt = data["dtStyle"]
                self.assertEqual(dt["color"], "rgb(83, 104, 121)")
                self.assertEqual(dt["fontWeight"], "700")
                self.assertLess(float(dt["fontSize"].replace("px", "")), float(style["fontSize"].replace("px", "")))
                self.assertEqual(data["badgeInfo"]["className"], "badge intake")
                self.assertEqual(data["badgeInfo"]["textContent"], "접수")
                self.assertEqual(data["badgeInfo"]["backgroundColor"], "rgb(236, 232, 251)")
                self.assertEqual(data["badgeInfo"]["color"], "rgb(74, 58, 153)")
                self.assertEqual(data["times"], [
                    ["2026-11-18T10:00:00+09:00", "2026-11-18 10:00"],
                    ["2026-11-18T12:00:00+09:00", "2026-11-18 12:00"],
                ])
                self.assertEqual(data["wideStyle"]["whiteSpace"], "pre-wrap")
                self.assertEqual(data["wideStyle"]["overflowWrap"], "anywhere")
                self.assertEqual(data["histHeaders"], ["시각", "단계", "메모", "다음 할 일"])
                self.assertTrue(all(data["noOverflow"]))
                self.assertTrue(all(data["noOverflowAfterPlain"]))
                self.assertEqual(data["plain"]["dts"], ["봇", "현재 단계", "상태", "시작", "마지막 업데이트", "이벤트 수"])
                self.assertEqual(data["plain"]["dds"], ["QA", "완료", "완료", "2026-11-18 09:00", "2026-11-18 11:00", "1개"])
                self.assertFalse(data["plain"]["hasTitle"])
                self.assertFalse(data["plain"]["hasWide"])
                self.assertEqual(data["plain"]["heading"], "이벤트 이력")
                self.assertTrue(data["plain"]["orderOk"])

    def test_history_summary_search_hit_heading_and_highlight(self):
        extra = """
        snapshot.tasks = [
          {task_id: "need-task", bot: "Engineering", title: "Plain Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T12:00:00+09:00", note: "needle note", next: "needle next",
           events: [
             {task_id: "need-task", bot: "Engineering", title: "Plain Title", stage: "코딩", note: "plain", next: "plain", ts: "2026-11-18T09:00:00+09:00"},
             {task_id: "need-task", bot: "Engineering", title: "Plain Title", stage: "코딩", note: "needle note", next: "needle next", ts: "2026-11-18T12:00:00+09:00"}
           ]},
          {task_id: "summary-only", bot: "Engineering", title: "SummaryOnlyKeyword Title", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T11:00:00+09:00", note: "SummaryOnlyKeyword note", next: "plain",
           events: [
             {task_id: "summary-only", bot: "Engineering", title: "Plain Title", stage: "코딩", note: "plain", next: "plain", ts: "2026-11-18T11:00:00+09:00"}
           ]},
          {task_id: "plain-task", bot: "Engineering", title: "Plain", stage: "코딩", status: "진행 중",
           started: "2026-11-18T09:00:00+09:00", updated: "2026-11-18T10:00:00+09:00", note: "plain", next: "plain",
           events: [{task_id: "plain-task", bot: "Engineering", title: "Plain", stage: "코딩", note: "plain", next: "plain", ts: "2026-11-18T10:00:00+09:00"}]}
        ];
        """
        exercise = """
            const table = document.querySelector(".tasks");
            const searchEl = document.getElementById("search");
            const applySearch = (query) => {
              searchEl.value = query;
              searchEl.dispatchEvent(new Event("input", {bubbles: true}));
              searchEl.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter", bubbles: true, cancelable: true}));
            };
            const ids = () => [...document.querySelectorAll(".task-toggle")].map(node => node.dataset.taskId);
            applySearch("needle");
            observations.needleIds = ids();
            const hist = document.querySelector(".history");
            observations.heading = hist ? hist.querySelector("h2").textContent : null;
            observations.summaryAfterH2 = hist ? hist.querySelector("h2").nextElementSibling.classList.contains("history-summary") : null;
            observations.tableAfterSummary = hist ? hist.querySelector(".history-summary").nextElementSibling.tagName : null;
            observations.summaryMarks = hist ? [...hist.querySelector(".history-summary").querySelectorAll("mark")].map(node => node.textContent) : [];
            observations.summaryText = hist ? hist.querySelector(".history-summary").textContent : null;
            observations.histHeaders = hist ? [...hist.querySelector("table").tHead.rows[0].cells].map(th => th.textContent) : [];
            observations.noOverflow = [document.documentElement.scrollWidth <= window.innerWidth,
              document.body.scrollWidth <= window.innerWidth];
            applySearch("SummaryOnlyKeyword");
            observations.summaryOnlyIds = ids();
            observations.summaryOnlyEmpty = document.querySelector("td.empty") ? document.querySelector("td.empty").textContent : null;
            applySearch("");
            observations.clearedIds = ids();
            const toggle = [...document.querySelectorAll(".task-toggle")].find(node => node.dataset.taskId === "need-task");
            toggle.click();
            const full = document.querySelector(".history");
            observations.fullHeading = full ? full.querySelector("h2").textContent : null;
            observations.fullSummaryAfterH2 = full ? full.querySelector("h2").nextElementSibling.classList.contains("history-summary") : null;
            observations.fullMarks = full ? full.querySelector(".history-summary").querySelectorAll("mark").length : -1;
            applySearch("Engineering");
            observations.engIds = ids();
            const engHist = document.querySelector(".history");
            observations.engSummaryMarks = engHist ? [...engHist.querySelector(".history-summary").querySelectorAll("mark")].map(node => node.textContent) : [];
        """
        for width in (1280, 390):
            with self.subTest(width=width):
                data = self.observe_page(exercise, width, extra_setup=extra)
                self.assertEqual(data["needleIds"], ["need-task"])
                self.assertEqual(data["heading"], "일치한 이벤트 1개 (전체 2개)")
                self.assertTrue(data["summaryAfterH2"])
                self.assertEqual(data["tableAfterSummary"], "TABLE")
                self.assertIn("needle", data["summaryMarks"])
                self.assertIn("needle note", data["summaryText"])
                self.assertEqual(data["histHeaders"], ["시각", "단계", "메모", "다음 할 일"])
                self.assertTrue(all(data["noOverflow"]))
                # 요약만으로 매칭하면 안 된다: 검색 대상은 events만이다.
                self.assertEqual(data["summaryOnlyIds"], [])
                self.assertEqual(data["summaryOnlyEmpty"], "일치하는 이벤트가 없습니다.")
                self.assertEqual(data["clearedIds"], ["need-task", "plain-task", "summary-only"])
                self.assertEqual(data["fullHeading"], "이벤트 이력")
                self.assertTrue(data["fullSummaryAfterH2"])
                self.assertEqual(data["fullMarks"], 0)
                self.assertEqual(data["engIds"], ["need-task", "plain-task", "summary-only"])
                self.assertIn("Engineering", data["engSummaryMarks"])


class MobileStaticTests(unittest.TestCase):
    def test_viewport_meta(self):
        self.assertIn("width=device-width", server.PAGE)
        self.assertIn("initial-scale=1", server.PAGE)
        self.assertIn("viewport-fit=cover", server.PAGE)
        self.assertIn('<meta name="theme-color" content="#193247">', server.PAGE)
        self.assertNotIn("apple-mobile-web-app-capable", server.PAGE)
        self.assertIn("-webkit-text-size-adjust", server.PAGE)
        self.assertIn("100%", server.PAGE)

    def test_mobile_css_block_and_safe_area(self):
        self.assertIn("@media (max-width: 640px)", server.PAGE)
        self.assertIn("@media (max-width: 700px)", server.PAGE)
        idx = server.PAGE.index("@media (max-width: 640px)")
        block = server.PAGE[idx:]
        for inset in ("safe-area-inset-top", "safe-area-inset-left",
                      "safe-area-inset-right", "safe-area-inset-bottom"):
            self.assertIn("env(" + inset, block)
            self.assertIn("env(" + inset + ", 0", block)
        # Bottom sheet requirements.
        self.assertIn("position: fixed", block)
        self.assertIn("max-height: 70vh", block)
        self.assertIn("70dvh", block)
        self.assertIn("border-radius: 16px 16px 0 0", block)
        self.assertIn("overscroll-behavior: contain", block)
        self.assertIn("calc(12px + env(safe-area-inset-bottom", block)
        self.assertIn("calc(12px + env(safe-area-inset-left", block)
        self.assertIn("calc(12px + env(safe-area-inset-right", block)
        # Title must stay first in the stacked card regardless of column order.
        self.assertIn("order: -1", block)
        self.assertIn('td[data-column="title"]', block)
        self.assertIn("td.title-cell", block)
        self.assertIn("td:has(.task-toggle)", block)
        # Label/value rows align with a fixed-width label column.
        self.assertIn("display: inline-block", block)
        self.assertIn("6.5rem", block)
        self.assertIn("flex: 0 0 6.5rem", block)
        self.assertIn("width: 6.5rem", block)
        label_idx = block.index(".tasks tbody tr.task-row > td::before")
        label_rule = block[label_idx:block.index("}", label_idx)]
        self.assertNotIn("float:", label_rule)
        self.assertIn("display: inline-block", label_rule)
        self.assertIn("6.5rem", label_rule)
        # Card requirements.
        self.assertIn("min-width: 0", block)
        self.assertIn("content: attr(data-label)", block)
        self.assertIn("clip-path: inset(50%)", block)
        self.assertIn("border-radius: 12px", block)
        self.assertIn("margin: 10px", block)
        self.assertIn("touch-action: manipulation", server.PAGE)
        self.assertIn("-webkit-tap-highlight-color: transparent", server.PAGE)
        self.assertIn(":active", block)
        self.assertIn("prefers-reduced-motion", server.PAGE)
        self.assertIn("font-size: 16px", block)
        self.assertIn("font-size: 1rem", block)
        self.assertIn("-webkit-overflow-scrolling: touch", block)
        self.assertIn("min-height: 44px", block)
        # thead must not be display:none anywhere in the mobile block.
        self.assertNotIn("thead { display: none", block)
