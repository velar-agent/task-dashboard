#!/usr/bin/env python3
"""Engineering 작업 대시보드."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from operator import itemgetter
from urllib.parse import unquote


DEFAULT_TASKS_FILE = "/workspace/task-status/tasks.jsonl"
KST = timezone(timedelta(hours=9), "KST")
STAGES = ("계획", "코딩", "리뷰", "커밋", "배포", "점검", "완료", "막힘")
LOGGER = logging.getLogger(__name__)


def read_event(line):
    record = json.loads(line.decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("이벤트는 JSON 객체여야 합니다.")

    def field(key):
        value = record.get(key)
        return value if isinstance(value, str) and value.strip() else ""

    event = {key: field(key) for key in ("task_id", "bot", "title", "stage", "note", "next", "ts")}
    if not event["task_id"] or event["stage"] not in STAGES:
        raise ValueError("작업 ID 또는 단계가 올바르지 않습니다.")
    ts = event["ts"]
    moment = datetime.fromisoformat(ts[:-1] + "+00:00" if ts.endswith("Z") else ts)
    if moment.tzinfo is None:
        raise ValueError("시각에 시간대가 필요합니다.")
    event["ts"] = moment.astimezone(KST).isoformat()
    return event


def load_tasks(path):
    """유효한 이벤트를 시각순으로 접고 최근 업데이트된 작업부터 반환한다."""
    groups = {}
    skipped = 0
    first_invalid = None
    try:
        stream = open(path, "rb")
    except FileNotFoundError:
        return []

    with stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = read_event(line)
            except (ValueError, OverflowError, RecursionError):
                skipped += 1
                if first_invalid is None:
                    first_invalid = number
                continue
            groups.setdefault(event["task_id"], []).append(event)
    if skipped:
        LOGGER.warning("%s에서 잘못된 이벤트 %d줄을 건너뜁니다 (첫 오류 %d행).",
                       path, skipped, first_invalid)

    tasks = []
    for task_id, events in groups.items():
        # 같은 오프셋으로 정규화된 ISO 시각이다. 안정 정렬이 동률의 파일 순서를 보존한다.
        events.sort(key=itemgetter("ts"))
        title = bot = ""
        for event in events:
            title = event["title"] or title
            bot = event["bot"] or bot
        latest = events[-1]
        stage = latest["stage"]
        tasks.append({
            "task_id": task_id,
            "bot": bot,
            "title": title,
            "stage": stage,
            "status": stage if stage in ("완료", "막힘") else "진행 중",
            "started": events[0]["ts"],
            "updated": latest["ts"],
            "note": latest["note"],
            "next": latest["next"],
            "events": events,
        })
    return sorted(tasks, key=itemgetter("updated"), reverse=True)


PAGE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>작업 현황 · Engineering</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #172d40;
      --muted: #536879;
      --line: #dae2e8;
      --accent: #225c8a;
      --mono: "IBM Plex Mono", ui-monospace, monospace;
      font-family: Inter, "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif;
      color: var(--ink);
      background: #f4f7f9;
    }
    * { box-sizing: border-box; }
    body { margin: 0; }
    button { color: inherit; font: inherit; cursor: pointer; }
    :focus-visible { outline: 3px solid #2985bd; outline-offset: 3px; }
    .wrap { width: min(1480px, calc(100% - 64px)); margin-inline: auto; }
    .masthead { color: #f4f8fa; background: #193247; }
    .masthead .wrap, .heading, .refresh-info, .brand, .auto {
      display: flex; align-items: center; gap: 12px;
    }
    .masthead .wrap { min-height: 62px; justify-content: space-between; }
    .brand { font-weight: 650; letter-spacing: .015em; }
    .mark { display: inline-flex; gap: 3px; align-items: flex-end; height: 19px; }
    .mark i { width: 5px; height: 9px; border-radius: 1px; background: #a5ded4; }
    .mark i:nth-child(2) { height: 14px; }
    .mark i:nth-child(3) { height: 19px; }
    .auto { gap: 7px; color: #d3dfe7; font-size: .8rem; }
    main { padding-block: 38px 56px; }
    .heading { justify-content: space-between; flex-wrap: wrap; margin-bottom: 20px; }
    h1 { margin: 0; font-size: clamp(1.7rem, 3vw, 2.15rem); letter-spacing: -.055em; }
    .refresh-info { flex-wrap: wrap; color: var(--muted); font-size: .8rem; }
    time { white-space: nowrap; font: .82rem var(--mono); font-variant-numeric: tabular-nums; }
    .refresh {
      padding: 7px 12px; border: 1px solid #cbd7df; border-radius: 7px;
      background: white; font-weight: 600;
    }
    .refresh:hover { background: #edf4f7; }
    .refresh:disabled { opacity: .6; cursor: progress; }
    .board { background: white; border: 1px solid var(--line); border-radius: 12px; }
    .filters { display: flex; gap: 5px; padding: 14px; overflow-x: auto; }
    .filter {
      display: inline-flex; align-items: center; gap: 7px; white-space: nowrap;
      border: 0; background: transparent; color: var(--muted); border-radius: 6px;
      padding: 8px 12px; font-size: .9rem; font-weight: 600;
    }
    .filter:hover { background: #f1f5f8; }
    .filter[aria-pressed="true"] { background: #e4eff7; color: #194f78; }
    .count { min-width: 1.4em; text-align: center; font: .78rem var(--mono); }
    .scroll { overflow-x: auto; border-radius: 0 0 12px 12px; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; text-align: left; }
    .tasks { min-width: 1160px; }
    .col-title { width: 23%; } .col-bot { width: 10%; } .col-stage { width: 8%; }
    .col-time { width: 13%; } .col-note { width: 18%; } .col-next { width: 15%; }
    th, td { padding: 15px 12px; overflow-wrap: anywhere; }
    th { background: #f2f5f7; color: var(--muted); font-size: .8rem; font-weight: 650; }
    .tasks > thead th { border-block: 1px solid var(--line); }
    td { border-bottom: 1px solid var(--line); font-size: .88rem; line-height: 1.55; vertical-align: top; }
    .tasks > tbody > tr:last-child > td { border-bottom: 0; }
    .task-row { cursor: pointer; }
    .task-row:hover, .task-row:focus-within, .task-row.expanded { background: #f6fafc; }
    .task-toggle {
      display: flex; align-items: baseline; gap: 9px; padding: 0;
      border: 0; background: transparent; text-align: left; font-weight: 650;
    }
    .caret {
      flex: 0 0 7px; height: 7px; border: solid var(--accent); border-width: 0 1.5px 1.5px 0;
      transform: translateY(-2px) rotate(-45deg);
    }
    .expanded .caret { transform: translateY(-4px) rotate(45deg); }
    .task-id { display: block; color: var(--muted); margin-top: 3px; font: .73rem/1.4 var(--mono); }
    .free-text { white-space: pre-wrap; }
    .badge {
      display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
      border-radius: 5px; padding: 2px 8px; font-size: .78rem; font-weight: 650;
      background: #e3effb; color: #215a8a;
    }
    .badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    .badge.check { background: #fff1d1; color: #80550d; }
    .badge.done { background: #def2e6; color: #206442; }
    .badge.blocked { background: #fbe5e5; color: #a12f36; }
    .history > td { padding: 15px 25px 20px; background: #f4f8fa; }
    .history h2 { margin: 0 0 9px; color: #385568; font-size: .9rem; }
    .history th, .history td { padding: 8px 10px; }
    .history table { border-left: 2px solid #b5cddb; }
    .history th { background: #e9eff3; }
    .history th:first-child { width: 170px; } .history th:nth-child(2) { width: 90px; }
    .empty { padding: 44px 18px; color: var(--muted); text-align: center; }
    .notice { padding: 11px 14px; border-radius: 7px; color: #733f10; background: #fff0d7; }
    @media (max-width: 700px) {
      .wrap { width: calc(100% - 24px); }
      main { padding-top: 24px; }
      .heading { align-items: flex-start; flex-direction: column; gap: 10px; }
      .filters { gap: 2px; padding: 9px 6px; }
      .filter { padding: 8px; gap: 4px; }
      .empty { text-align: left; }
    }
  </style>
</head>
<body>
  <header class="masthead"><div class="wrap">
    <div class="brand"><span class="mark" aria-hidden="true"><i></i><i></i><i></i></span>Engineering</div>
    <span class="auto">30초마다 자동 갱신</span>
  </div></header>
  <main class="wrap">
    <div class="heading">
      <h1>작업 현황</h1>
      <div class="refresh-info">
        <span>마지막 갱신 (KST) <time id="updated">—</time></span>
        <button type="button" id="refresh" class="refresh">새로고침</button>
      </div>
    </div>
    <p id="notice" class="notice" role="alert" hidden>작업 목록을 불러오지 못했습니다. 새로고침으로 다시 시도할 수 있습니다.</p>
    <noscript>작업 목록을 보려면 자바스크립트를 켜 주세요.</noscript>
    <section class="board" aria-label="Engineering 작업 목록">
      <div class="filters" role="group" aria-label="작업 상태 필터">
        <button class="filter" type="button" data-status="전체" aria-pressed="true">전체 <span class="count">—</span></button>
        <button class="filter" type="button" data-status="진행 중" aria-pressed="false">진행 중 <span class="count">—</span></button>
        <button class="filter" type="button" data-status="완료" aria-pressed="false">완료 <span class="count">—</span></button>
        <button class="filter" type="button" data-status="막힘" aria-pressed="false">막힘 <span class="count">—</span></button>
      </div>
      <div class="scroll" tabindex="0" role="region" aria-label="작업 표">
        <table class="tasks" aria-label="작업 현황">
          <colgroup>
            <col class="col-title"><col class="col-bot"><col class="col-stage">
            <col class="col-time"><col class="col-time"><col class="col-note"><col class="col-next">
          </colgroup>
          <thead><tr>
            <th scope="col">작업</th><th scope="col">봇</th><th scope="col">현재 단계</th>
            <th scope="col">시작</th><th scope="col" aria-sort="descending">마지막 업데이트</th>
            <th scope="col">메모</th><th scope="col">다음 할 일</th>
          </tr></thead>
          <tbody id="rows"><tr><td class="empty" colspan="7">작업을 불러오는 중입니다.</td></tr></tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    "use strict";
    const rows = document.getElementById("rows");
    const filters = document.querySelectorAll(".filter");
    const refreshButton = document.getElementById("refresh");
    const notice = document.getElementById("notice");
    const updated = document.getElementById("updated");
    let tasks = [];
    let selected = "전체";
    let expandedId = null;
    let loaded = false;

    function element(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function setTime(node, iso) {
      // API 시각은 모두 +09:00이므로 브라우저의 시간대와 무관하다.
      node.dateTime = iso;
      node.textContent = iso.slice(0, 16).replace("T", " ");
    }

    function timeCell(iso) {
      const cell = element("td");
      const time = element("time");
      setTime(time, iso);
      cell.append(time);
      return cell;
    }

    function stageCell(stage) {
      const styles = {완료: "done", 막힘: "blocked", 계획: "check", 리뷰: "check", 점검: "check"};
      const cell = element("td");
      cell.append(element("span", "badge " + (styles[stage] || ""), stage));
      return cell;
    }

    function textCell(value) {
      return element("td", "free-text", value || "—");
    }

    function historyRow(task, id) {
      const row = element("tr", "history");
      row.id = id;
      const cell = element("td");
      cell.colSpan = 7;
      const table = element("table");
      table.setAttribute("aria-label", "이벤트 이력");
      const header = table.createTHead().insertRow();
      for (const label of ["시각", "단계", "메모", "다음 할 일"]) {
        const th = element("th", "", label);
        th.scope = "col";
        header.append(th);
      }
      const body = table.createTBody();
      for (const event of task.events) {
        body.insertRow().append(timeCell(event.ts), stageCell(event.stage),
                                textCell(event.note), textCell(event.next));
      }
      cell.append(element("h2", "", "이벤트 이력"), table);
      row.append(cell);
      return row;
    }

    function emptyRow(message) {
      const row = element("tr");
      const cell = element("td", "empty", message);
      cell.colSpan = 7;
      row.append(cell);
      return row;
    }

    function render() {
      for (const button of filters) {
        const status = button.dataset.status;
        const count = status === "전체" ? tasks.length : tasks.filter(t => t.status === status).length;
        button.querySelector(".count").textContent = loaded ? count : "—";
        button.setAttribute("aria-pressed", String(status === selected));
      }
      if (!loaded) return;
      const visible = tasks.filter(task => selected === "전체" || task.status === selected);
      const fragment = document.createDocumentFragment();
      const focusedId = document.activeElement?.dataset.taskId;
      let restoreFocus;
      for (const [index, task] of visible.entries()) {
        const expanded = task.task_id === expandedId;
        const row = element("tr", expanded ? "task-row expanded" : "task-row");
        const first = element("td");
        const toggle = element("button", "task-toggle");
        toggle.type = "button";
        toggle.dataset.taskId = task.task_id;
        toggle.setAttribute("aria-expanded", String(expanded));
        const label = element("span", "", task.title || task.task_id);
        if (task.title) label.append(element("small", "task-id", task.task_id));
        const caret = element("span", "caret");
        caret.setAttribute("aria-hidden", "true");
        toggle.append(caret, label);
        first.append(toggle);
        row.append(first, textCell(task.bot), stageCell(task.stage), timeCell(task.started),
                   timeCell(task.updated), textCell(task.note), textCell(task.next));
        row.addEventListener("click", () => {
          expandedId = expanded ? null : task.task_id;
          render();
        });
        fragment.append(row);
        if (expanded) {
          const id = "history-" + index;
          toggle.setAttribute("aria-controls", id);
          fragment.append(historyRow(task, id));
        }
        if (task.task_id === focusedId) restoreFocus = toggle;
      }
      if (!visible.length) {
        fragment.append(emptyRow(selected === "전체" ? "등록된 작업이 없습니다." : "이 상태의 작업이 없습니다."));
      }
      rows.replaceChildren(fragment);
      if (restoreFocus) restoreFocus.focus({preventScroll: true});
    }

    for (const button of filters) {
      button.addEventListener("click", () => {
        selected = button.dataset.status;
        render();
      });
    }

    async function refresh() {
      if (refreshButton.disabled) return;
      refreshButton.disabled = true;
      try {
        const response = await fetch("/api/tasks", {cache: "no-store"});
        if (!response.ok) throw new Error("목록 조회 실패");
        const data = await response.json();
        tasks = data.tasks.filter(task => task.bot === "Engineering");
        loaded = true;
        render();
        setTime(updated, data.generated_at);
        notice.hidden = true;
      } catch {
        notice.hidden = false;
        if (!loaded) rows.replaceChildren(emptyRow("작업 목록을 불러올 수 없습니다."));
      } finally {
        refreshButton.disabled = false;
      }
    }

    refreshButton.addEventListener("click", refresh);
    refresh();
    window.setInterval(refresh, 30000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def send_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, value):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8", "backslashreplace")
        self.send_body(status, "application/json; charset=utf-8", body)

    def do_GET(self):
        path = self.path.partition("?")[0]
        if path == "/":
            self.send_body(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            return
        if path == "/healthz":
            self.send_json(200, {"status": "ok"})
            return
        prefix = "/api/tasks/"
        if path != "/api/tasks" and not path.startswith(prefix):
            self.send_json(404, {"error": "경로를 찾을 수 없습니다."})
            return

        try:
            tasks = load_tasks(os.environ.get("TASKS_FILE", DEFAULT_TASKS_FILE))
        except OSError as exc:
            LOGGER.error("이벤트 파일을 읽을 수 없습니다. %s", exc)
            self.send_json(503, {"error": "이벤트 파일을 읽을 수 없습니다."})
            return
        if path == "/api/tasks":
            self.send_json(200, {"tasks": tasks, "generated_at": datetime.now(KST).isoformat(timespec="seconds")})
            return
        task_id = unquote(path[len(prefix):])
        for task in tasks:
            if task["task_id"] == task_id:
                self.send_json(200, task)
                return
        self.send_json(404, {"error": "작업을 찾을 수 없습니다."})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "0.0.0.0")
    with ThreadingHTTPServer((host, port), DashboardHandler) as server:
        print(f"Engineering 작업 대시보드가 {server.server_address[1]} 포트에서 실행 중입니다.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
