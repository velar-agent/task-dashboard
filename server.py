#!/usr/bin/env python3
"""봇 작업 현황 대시보드."""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from operator import itemgetter
from urllib.parse import unquote


DEFAULT_TASKS_FILE = "/workspace/task-status/tasks.jsonl"
KST = timezone(timedelta(hours=9), "KST")
STAGES = ("접수", "계획", "코딩", "리뷰", "커밋", "배포", "점검", "완료", "막힘")
LOGGER = logging.getLogger(__name__)

# 기본 표시 순서: 상태 그룹(진행 중 → 막힘 → 완료), 그룹 안은 task_id
# 자연 정렬, task_id 동률은 마지막 업데이트 내림차순이다.
_STATUS_ORDER = {"진행 중": 0, "막힘": 1, "완료": 2}
_DIGIT_CHUNK_RE = re.compile(r"(\d+)")


def _task_status(task):
    """작업의 표시 상태를 반환한다.

    ``task.get("status")``가 비어 있지 않은 문자열이면 그 값을 쓰고,
    없으면 ``stage``에서 유도한다(완료→완료, 막힘→막힘, 그 외 진행 중).
    접수부터 점검까지의 단계는 모두 진행 중이다.
    """
    status = task.get("status") if isinstance(task, dict) else None
    if isinstance(status, str) and status:
        return status
    stage = task.get("stage") if isinstance(task, dict) else None
    if stage == "완료":
        return "완료"
    if stage == "막힘":
        return "막힘"
    return "진행 중"


def _natural_task_id_key(task_id):
    """task_id용 자연 정렬 키를 만든다.

    클라이언트 ``localeCompare(..., "ko", {numeric: true, sensitivity: "base"})``를
    Python 표준 라이브러리로 근사한다: 숫자/비숫자 청크로 나누어 숫자 청크는
    int로, 비숫자 청크는 소문자로 비교한다. JS 로케일 비교와 마찬가지로
    같은 위치에서는 숫자 청크가 비숫자 청크보다 앞에 온다. 한국어 로케일
    순서나 악센트 무시 같은 세부 규칙은 완전히 일치하지 않을 수 있다.
    """
    key = []
    for chunk in _DIGIT_CHUNK_RE.split(str(task_id)):
        if not chunk:
            continue
        if chunk.isdigit():
            key.append((0, int(chunk)))
        else:
            key.append((1, chunk.lower()))
    return key


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
    """유효한 이벤트를 시각순으로 접고 상태 그룹→task_id 자연 정렬→updated 내림차순으로 반환한다.

    상태 그룹 순서는 진행 중, 막힘, 완료이며 완료는 항상 마지막이다. 같은
    상태 그룹 안에서는 task_id를 자연 정렬(숫자는 수치 비교, 대소문자 무시)로
    오름차순 정렬하고, task_id 비교가 동률이면 마지막 업데이트를 내림차순으로
    정렬한다. Python 자연 정렬 키는 클라이언트의 JS 로케일 비교를 근사한다.
    """
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
    # updated 내림차순을 먼저 정렬한 뒤 상태/task_id로 안정 정렬한다. task_id
    # 비교가 동률인 작업은 updated 내림차순 순서를 유지한다.
    tasks.sort(key=itemgetter("updated"), reverse=True)
    tasks.sort(key=lambda task: (_STATUS_ORDER.get(_task_status(task), 0),
                                 _natural_task_id_key(task["task_id"])))
    return tasks


PAGE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#193247">
  <title>봇 작업 현황</title>
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
      -webkit-text-size-adjust: 100%;
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    body { margin: 0; }
    button { color: inherit; font: inherit; cursor: pointer; touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
    :focus-visible { outline: 3px solid #2985bd; outline-offset: 3px; }
    .wrap {
      width: min(1480px, calc(100% - 64px)); margin-inline: auto;
      padding-left: env(safe-area-inset-left, 0px); padding-right: env(safe-area-inset-right, 0px);
    }
    .masthead {
      color: #f4f8fa; background: #193247;
      padding-top: env(safe-area-inset-top, 0px);
      padding-left: env(safe-area-inset-left, 0px); padding-right: env(safe-area-inset-right, 0px);
    }
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
    main { padding-block: 38px calc(56px + env(safe-area-inset-bottom, 0px)); }
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
    .filter-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 16px; padding: 14px; }
    .bot-filter { display: flex; align-items: center; gap: 7px; color: var(--muted); font-size: .9rem; font-weight: 600; }
    .bot-filter select {
      max-width: min(15rem, 65vw); padding: 6px 8px; border: 1px solid #cbd7df; border-radius: 6px;
      background: white; color: var(--ink); font: inherit;
    }
    .filters { display: flex; gap: 5px; overflow-x: auto; }
    .filter {
      display: inline-flex; align-items: center; gap: 7px; white-space: nowrap;
      border: 0; background: transparent; color: var(--muted); border-radius: 6px;
      padding: 8px 12px; font-size: .9rem; font-weight: 600;
    }
    .filter:hover { background: #f1f5f8; }
    .filter[aria-pressed="true"] { background: #e4eff7; color: #194f78; }
    .count { min-width: 1.4em; text-align: center; font: .78rem var(--mono); }
    .visually-hidden {
      position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
      overflow: hidden; clip: rect(0, 0, 0, 0); clip-path: inset(50%);
      white-space: nowrap; border: 0;
    }
    .search-wrap { display: flex; align-items: center; gap: 8px; flex: 1 1 100%; min-width: 0; max-width: 100%; }
    .search-wrap input[type="search"] {
      flex: 1 1 auto; min-width: 0; max-width: 100%;
      padding: 8px 12px; border: 1px solid #cbd7df; border-radius: 7px;
      background: white; color: var(--ink); font: inherit; font-size: 16px;
    }
    .search-wrap input[type="search"]::-webkit-search-cancel-button { -webkit-appearance: none; appearance: none; }
    .search-clear {
      flex: 0 0 auto; width: 32px; height: 32px; padding: 0;
      border: 1px solid #cbd7df; border-radius: 7px; background: white;
      font-weight: 600; line-height: 1;
    }
    .search-clear[hidden] { display: none; }
    .search-status { flex: 1 1 100%; margin: 0; color: var(--muted); font-size: .85rem; }
    .search-status[hidden] { display: none; }
    mark { background: #fff1a8; color: inherit; border-radius: 2px; }
    .column-menu { position: relative; margin-left: auto; }
    .columns-button {
      padding: 7px 12px; border: 1px solid #cbd7df; border-radius: 7px;
      background: white; font-weight: 600; white-space: nowrap;
    }
    .columns-button:hover { background: #edf4f7; }
    .columns-panel {
      position: absolute; right: 0; top: calc(100% + 6px); z-index: 20;
      min-width: 15rem; max-width: min(19rem, calc(100vw - 24px));
      padding: 8px; border: 1px solid var(--line); border-radius: 10px;
      background: white; box-shadow: 0 12px 32px rgb(23 45 64 / .18);
    }
    .columns-panel[hidden] { display: none; }
    .column-item { display: flex; align-items: center; gap: 4px; }
    .columns-panel label {
      display: flex; align-items: center; gap: 8px; padding: 7px 8px;
      border-radius: 6px; font-size: .9rem; font-weight: 550; cursor: pointer;
      flex: 1 1 auto; min-width: 0;
    }
    .columns-panel label span { min-width: 0; overflow-wrap: anywhere; }
    .columns-panel label:hover { background: #f1f5f8; }
    .columns-panel input { width: 1rem; height: 1rem; accent-color: var(--accent); flex: 0 0 auto; }
    .columns-panel input:disabled { cursor: not-allowed; opacity: .5; }
    .column-move {
      flex: 0 0 28px; width: 28px; height: 28px; padding: 0;
      border: 1px solid #cbd7df; border-radius: 6px; background: white;
      font-size: .8rem; line-height: 1;
    }
    .column-move:hover:not(:disabled) { background: #edf4f7; }
    .column-move:disabled { cursor: not-allowed; opacity: .5; }
    .reset-order {
      width: 100%; margin-top: 6px; padding: 7px 12px;
      border: 1px solid #cbd7df; border-radius: 7px; background: white; font-weight: 600;
    }
    .reset-order:hover:not(:disabled) { background: #edf4f7; }
    .reset-order:disabled { cursor: not-allowed; opacity: .5; }
    .scroll { overflow-x: auto; border-radius: 0 0 12px 12px; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; text-align: left; }
    .tasks { min-width: 1160px; }
    .col-bot { width: 10%; } .col-stage { width: 8%; } .col-title { width: 23%; }
    .col-time { width: 13%; } .col-note { width: 18%; } .col-next { width: 15%; }
    th, td { padding: 15px 12px; overflow-wrap: anywhere; }
    th { background: #f2f5f7; color: var(--muted); font-size: .8rem; font-weight: 650; }
    th[draggable="true"] { cursor: grab; }
    th.dragging { opacity: .5; }
    th.drop-target { box-shadow: inset 0 0 0 2px var(--accent); }
    .tasks > thead th { border-block: 1px solid var(--line); }
    td { border-bottom: 1px solid var(--line); font-size: .88rem; line-height: 1.55; vertical-align: top; }
    .tasks > tbody > tr:last-child > td { border-bottom: 0; }
    .task-row { cursor: pointer; -webkit-tap-highlight-color: transparent; }
    .task-row:hover, .task-row:focus-within, .task-row.expanded { background: #f6fafc; }
    .task-row:active { background: #e8f0f5; }
    .refresh:active, .columns-button:active, .filter:active,
    .column-move:active, .reset-order:active, .task-toggle:active { background: #dbe7f0; }
    .task-toggle {
      display: flex; align-items: baseline; gap: 9px; padding: 0;
      border: 0; background: transparent; text-align: left; font-weight: 650;
    }
    .caret {
      flex: 0 0 7px; height: 7px; border: solid var(--accent); border-width: 0 1.5px 1.5px 0;
      transform: translateY(-2px) rotate(-45deg);
    }
    .expanded .caret { transform: translateY(-4px) rotate(45deg); }
    .task-label { display: flex; flex-direction: column; gap: 3px; align-items: flex-start; min-width: 0; }
    .task-id { display: block; font-weight: 700; overflow-wrap: anywhere; }
    .task-title { display: block; overflow-wrap: anywhere; font-weight: 400; }
    .free-text { white-space: pre-wrap; overflow-wrap: anywhere; }
    .columns-backdrop { display: none; }
    .columns-backdrop[hidden] { display: none; }
    .badge {
      display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;
      border-radius: 5px; padding: 2px 8px; font-size: .78rem; font-weight: 650;
      background: #e3effb; color: #215a8a;
    }
    .badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    .badge.intake { background: #ece8fb; color: #4a3a99; }
    .badge.check { background: #fff1d1; color: #80550d; }
    .badge.done { background: #def2e6; color: #206442; }
    .badge.blocked { background: #fbe5e5; color: #a12f36; }
    .history > td { padding: 15px 25px 20px; background: #f4f8fa; }
    .history h2 { margin: 0 0 9px; color: #385568; font-size: .9rem; }
    .history-summary {
      background: white; border: 1px solid #dae2e8; border-radius: 8px;
      padding: 10px 12px; margin: 0 0 10px; font-size: .85rem;
      box-sizing: border-box; max-width: 100%; min-width: 0; overflow-wrap: anywhere;
    }
    .history-summary .summary-id { display: block; font-weight: 700; overflow-wrap: anywhere; }
    .history-summary .summary-title { display: block; font-weight: 400; overflow-wrap: anywhere; }
    .history-summary dl {
      display: grid; grid-template-columns: auto minmax(0, 1fr) auto minmax(0, 1fr);
      gap: 6px 12px; margin: 8px 0 0; padding: 0;
      min-width: 0; max-width: 100%; box-sizing: border-box;
    }
    .history-summary dt { color: var(--muted); font-size: .78rem; font-weight: 700; line-height: 1.5; }
    .history-summary dd { margin: 0; min-width: 0; overflow-wrap: anywhere; line-height: 1.5; }
    .history-summary dd.summary-wide { grid-column: 2 / -1; white-space: pre-wrap; overflow-wrap: anywhere; }
    .history th, .history td { padding: 8px 10px; }
    .history table { border-left: 2px solid #b5cddb; }
    .history th { background: #e9eff3; }
    .history th:first-child { width: 170px; } .history th:nth-child(2) { width: 90px; }
    .history tbody tr.hit { box-shadow: inset 4px 0 0 var(--accent); }
    .empty { padding: 44px 18px; color: var(--muted); text-align: center; }
    .notice { padding: 11px 14px; border-radius: 7px; color: #733f10; background: #fff0d7; }
    @media (max-width: 700px) {
      .wrap { width: calc(100% - 24px); }
      main { padding-top: 24px; }
      .heading { align-items: flex-start; flex-direction: column; gap: 10px; }
      .filter-bar { padding: 9px 6px; }
      .filters { gap: 2px; }
      .filter { padding: 8px; gap: 4px; }
      .empty { text-align: left; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
    }
    @media (max-width: 640px) {
      .masthead { padding-top: env(safe-area-inset-top, 0px); }
      .masthead .wrap { min-height: 52px; }
      .auto { font-size: .75rem; }
      h1 { font-size: 1.5rem; }
      .heading { gap: 8px; }
      .refresh-info { flex-wrap: nowrap; width: 100%; }
      .refresh { min-height: 44px; min-width: 44px; padding: 10px 16px; touch-action: manipulation; }
      .filter-bar { flex-direction: column; align-items: stretch; gap: 8px; padding: 10px; }
      .search-wrap { width: 100%; flex-basis: 100%; }
      .search-wrap input[type="search"] { min-height: 44px; font-size: 16px; touch-action: manipulation; }
      .search-clear { width: 44px; height: 44px; min-width: 44px; min-height: 44px; touch-action: manipulation; }
      .bot-filter { width: 100%; min-height: 44px; }
      .bot-filter select { width: 100%; max-width: none; min-height: 44px; font-size: 16px; }
      .filters {
        width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch;
        flex-wrap: nowrap; scroll-snap-type: x proximity; padding-bottom: 4px;
      }
      .filter { flex: 0 0 auto; min-height: 44px; scroll-snap-align: start; touch-action: manipulation; }
      .column-menu { width: 100%; margin-left: 0; }
      .columns-button { width: 100%; min-height: 44px; touch-action: manipulation; }
      .columns-panel {
        position: fixed; left: 0; right: 0; bottom: 0; top: auto;
        width: auto; min-width: 0; max-width: none;
        max-height: 70vh; max-height: 70dvh;
        overflow-y: auto; overscroll-behavior: contain;
        border-radius: 16px 16px 0 0;
        padding: 8px 12px calc(12px + env(safe-area-inset-bottom, 0px));
        padding-bottom: calc(12px + env(safe-area-inset-bottom, 0px));
        padding-left: calc(12px + env(safe-area-inset-left, 0px)); padding-right: calc(12px + env(safe-area-inset-right, 0px));
        box-shadow: 0 -12px 32px rgb(23 45 64 / .25); z-index: 30;
      }
      .columns-backdrop {
        display: block; position: fixed; inset: 0;
        background: rgb(0 0 0 / .4); z-index: 25; border: 0; padding: 0;
      }
      .columns-backdrop[hidden] { display: none; }
      .columns-panel label { min-height: 44px; font-size: 1rem; }
      .columns-panel input { width: 1.25rem; height: 1.25rem; }
      .column-move { width: 44px; height: 44px; min-width: 44px; min-height: 44px; flex-basis: 44px; touch-action: manipulation; }
      .reset-order { min-height: 44px; touch-action: manipulation; }
      .board { border: 0; background: transparent; }
      .scroll { overflow-x: visible; overflow-y: visible; border-radius: 0; background: transparent; }
      .tasks { min-width: 0 !important; width: 100%; max-width: 100%; }
      .tasks colgroup, .tasks col { display: none; }
      .tasks, .tasks thead, .tasks tbody, .tasks tr, .tasks td { display: block; }
      .tasks thead {
        position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
        overflow: hidden; clip: rect(0, 0, 0, 0); clip-path: inset(50%);
        white-space: nowrap; border: 0;
      }
      .tasks tbody tr.task-row { display: flex; flex-direction: column; }
      .tasks tbody tr.task-row {
        border: 1px solid var(--line); border-radius: 12px; background: white;
        margin: 10px 10px; padding: 12px 14px; max-width: calc(100% - 20px);
        overflow-wrap: anywhere; -webkit-tap-highlight-color: transparent;
      }
      .tasks tbody tr.task-row:active { background: #e8f0f5; }
      .tasks tbody tr.task-row > td {
        display: flex; gap: 12px; align-items: flex-start;
        border-bottom: 0; padding: 7px 0;
        min-width: 0; max-width: 100%; overflow-wrap: anywhere;
      }
      .tasks tbody tr.task-row > td::before {
        content: attr(data-label);
        display: inline-block; flex: 0 0 6.5rem; width: 6.5rem; flex-shrink: 0;
        color: var(--muted); font-size: .78rem; font-weight: 650; line-height: 1.6;
        overflow-wrap: anywhere;
      }
      .tasks tbody tr.task-row > td > * { overflow-wrap: anywhere; min-width: 0; }
      .tasks tbody tr.task-row > td:has(.badge) > *,
      .tasks tbody tr.task-row > td:has(time) > * {
        flex: 0 1 auto;
      }
      .tasks tbody tr.task-row > td:has(.task-toggle),
      .tasks tbody tr.task-row > td[data-column="title"],
      .tasks tbody tr.task-row > td.title-cell {
        order: -1; display: block;
        min-height: 44px; padding: 4px 0 10px;
      }
      .tasks tbody tr.task-row > td:has(.task-toggle)::before,
      .tasks tbody tr.task-row > td[data-column="title"]::before,
      .tasks tbody tr.task-row > td.title-cell::before { content: none; display: none; }
      .task-toggle { width: 100%; min-height: 44px; touch-action: manipulation; }
      .task-id { overflow-wrap: anywhere; }
      .task-title { overflow-wrap: anywhere; white-space: normal; }
      .free-text { white-space: pre-wrap; overflow-wrap: anywhere; }
      .tasks td.empty {
        display: block; text-align: left; padding: 12px 14px; margin: 10px 10px;
        color: var(--muted); overflow-wrap: anywhere;
      }
      .tasks td.empty::before { content: none; display: none; }
      .history { display: block; margin: 0 10px 10px; }
      .history > td { display: block; border: 0; padding: 0; background: transparent; }
      .history > td::before { content: none; display: none; }
      .history h2 { margin: 0 0 8px; }
      .history-summary dl { grid-template-columns: auto minmax(0, 1fr); }
      .history table { display: block; border: 0; }
      .history thead {
        position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;
        overflow: hidden; clip: rect(0, 0, 0, 0); clip-path: inset(50%);
        white-space: nowrap; border: 0;
      }
      .history tbody { display: block; }
      .history tbody tr {
        display: flex; flex-wrap: wrap; background: white;
        border: 1px solid var(--line); border-radius: 10px;
        margin: 0 0 8px; padding: 8px 12px; overflow-wrap: anywhere;
      }
      .history tbody td {
        display: block; border-bottom: 0; padding: 6px 0;
        min-width: 0; overflow-wrap: anywhere;
      }
      .history tbody td::before {
        content: attr(data-label); float: left; max-width: 40%; margin-right: 8px;
        color: var(--muted); font-size: .78rem; font-weight: 650; line-height: 1.6;
        overflow-wrap: anywhere;
      }
      .history tbody td[data-label="시각"] { flex: 1 1 52%; min-width: 0; }
      .history tbody td[data-label="단계"] { flex: 1 1 38%; min-width: 0; }
      .history tbody td[data-label="메모"],
      .history tbody td[data-label="다음 할 일"] { flex: 1 1 100%; min-width: 0; }
      .refresh, .columns-button, .filter, .column-move, .reset-order {
        min-height: 44px; touch-action: manipulation; -webkit-tap-highlight-color: transparent;
      }
      .bot-filter select { min-height: 44px; }
      .task-id, .task-title, .tasks tbody tr.task-row > td { word-break: normal; }
    }
  </style>
</head>
<body>
  <header class="masthead"><div class="wrap">
    <div class="brand"><span class="mark" aria-hidden="true"><i></i><i></i><i></i></span>봇 대시보드</div>
    <span class="auto">30초마다 자동 갱신</span>
  </div></header>
  <main class="wrap">
    <div class="heading">
      <h1>봇 작업 현황</h1>
      <div class="refresh-info">
        <span>마지막 갱신 (KST) <time id="updated">—</time></span>
        <button type="button" id="refresh" class="refresh">새로고침</button>
      </div>
    </div>
    <p id="notice" class="notice" role="alert" hidden>작업 목록을 불러오지 못했습니다. 새로고침으로 다시 시도할 수 있습니다.</p>
    <noscript>작업 목록을 보려면 자바스크립트를 켜 주세요.</noscript>
    <section class="board" aria-label="봇 작업 목록">
      <div class="filter-bar">
        <div class="search-wrap">
          <label class="visually-hidden" for="search">이벤트 키워드 검색</label>
          <input type="search" id="search" placeholder="이벤트 키워드 검색 (공백으로 여러 단어, AND)" autocomplete="off" enterkeyhint="search" aria-label="이벤트 키워드 검색">
          <button type="button" id="search-clear" class="search-clear" aria-label="검색어 지우기" hidden>×</button>
        </div>
        <p id="search-status" class="search-status" role="status" aria-live="polite" hidden></p>
        <label class="bot-filter">봇 <select id="bot-filter"><option value="">전체</option></select></label>
        <div class="filters" role="group" aria-label="작업 상태 필터">
          <button class="filter" type="button" data-status="전체" aria-pressed="true">전체 <span class="count">—</span></button>
          <button class="filter" type="button" data-status="진행 중" aria-pressed="false">진행 중 <span class="count">—</span></button>
          <button class="filter" type="button" data-status="완료" aria-pressed="false">완료 <span class="count">—</span></button>
          <button class="filter" type="button" data-status="막힘" aria-pressed="false">막힘 <span class="count">—</span></button>
        </div>
        <div class="column-menu">
          <button type="button" id="columns-button" class="columns-button" aria-haspopup="true" aria-expanded="false" aria-controls="columns-panel">열 ▾</button>
          <div id="columns-panel" class="columns-panel" role="group" aria-label="표시할 열" hidden></div>
          <div id="columns-backdrop" class="columns-backdrop" hidden></div>
        </div>
      </div>
      <div class="scroll" tabindex="0" role="region" aria-label="작업 표">
        <table class="tasks" aria-label="작업 현황">
          <colgroup>
            <col class="col-bot"><col class="col-stage"><col class="col-title">
            <col class="col-time"><col class="col-time"><col class="col-note"><col class="col-next">
          </colgroup>
          <thead><tr>
            <th scope="col">봇</th><th scope="col">현재 단계</th><th scope="col">작업</th>
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
    const botFilter = document.getElementById("bot-filter");
    const refreshButton = document.getElementById("refresh");
    const notice = document.getElementById("notice");
    const updated = document.getElementById("updated");
    const table = document.querySelector(".tasks");
    const colgroup = table.querySelector("colgroup");
    const headRow = table.querySelector("thead tr");
    const columnsButton = document.getElementById("columns-button");
    const columnsPanel = document.getElementById("columns-panel");
    const columnsBackdrop = document.getElementById("columns-backdrop");
    const searchInput = document.getElementById("search");
    const searchClear = document.getElementById("search-clear");
    const searchStatus = document.getElementById("search-status");
    const COLUMNS = [
      {key: "bot", label: "봇", colClass: "col-bot", width: 10,
        cell: (task, expanded, keywords) => textCell(botName(task), keywords)},
      {key: "stage", label: "현재 단계", colClass: "col-stage", width: 8,
        cell: (task, expanded, keywords) => stageCell(task.stage, keywords)},
      {key: "title", label: "작업", colClass: "col-title", width: 23,
        cell: (task, expanded, keywords) => {
          const cell = element("td", "title-cell");
          const toggle = element("button", "task-toggle");
          toggle.type = "button";
          toggle.dataset.taskId = task.task_id;
          toggle.setAttribute("aria-expanded", String(expanded));
          const label = element("span", "task-label");
          const idSpan = element("span", "task-id");
          appendHighlightedText(idSpan, task.task_id, keywords);
          label.append(idSpan);
          if (task.title) {
            const titleSpan = element("span", "task-title");
            appendHighlightedText(titleSpan, task.title, keywords);
            label.append(titleSpan);
          }
          const caret = element("span", "caret");
          caret.setAttribute("aria-hidden", "true");
          toggle.append(caret, label);
          cell.append(toggle);
          return cell;
        }},
      {key: "started", label: "시작", colClass: "col-time", width: 13,
        cell: (task, expanded, keywords) => timeCell(task.started, keywords)},
      {key: "updated", label: "마지막 업데이트", colClass: "col-time", width: 13,
        ariaSort: "descending", cell: (task, expanded, keywords) => timeCell(task.updated, keywords)},
      {key: "note", label: "메모", colClass: "col-note", width: 18,
        cell: (task, expanded, keywords) => textCell(task.note, keywords)},
      {key: "next", label: "다음 할 일", colClass: "col-next", width: 15,
        cell: (task, expanded, keywords) => textCell(task.next, keywords)},
    ];
    const columnsByKey = new Map(COLUMNS.map(column => [column.key, column]));
    const STORAGE_KEY = "task-dashboard.columns";
    const ORDER_STORAGE_KEY = "task-dashboard.column-order";
    let tasks = [];
    let selected = "전체";
    let expandedId = null;
    let loaded = false;
    let searchQuery = "";
    let searchDebounce = null;

    function defaultVisibleKeys() {
      return COLUMNS.map(column => column.key);
    }

    function loadVisibleColumns() {
      let raw;
      try {
        raw = localStorage.getItem(STORAGE_KEY);
      } catch {
        return defaultVisibleKeys();
      }
      if (raw === null) return defaultVisibleKeys();
      try {
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return defaultVisibleKeys();
        const known = new Set(COLUMNS.map(column => column.key));
        const found = new Set(parsed.filter(value => typeof value === "string" && known.has(value)));
        if (!found.size) return defaultVisibleKeys();
        return COLUMNS.map(column => column.key).filter(key => found.has(key));
      } catch {
        return defaultVisibleKeys();
      }
    }

    let visible = new Set(loadVisibleColumns());

    function defaultOrder() {
      return COLUMNS.map(column => column.key);
    }

    function loadColumnOrder() {
      let raw;
      try {
        raw = localStorage.getItem(ORDER_STORAGE_KEY);
      } catch {
        return defaultOrder();
      }
      if (raw === null) return defaultOrder();
      try {
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return defaultOrder();
        const seen = new Set();
        const order = [];
        for (const value of parsed) {
          if (typeof value !== "string" || !columnsByKey.has(value) || seen.has(value)) continue;
          seen.add(value);
          order.push(value);
        }
        // 누락된 키는 기본 상대 순서대로 뒤에 붙인다.
        for (const key of defaultOrder()) {
          if (!seen.has(key)) order.push(key);
        }
        return order;
      } catch {
        return defaultOrder();
      }
    }

    let columnOrder = loadColumnOrder();
    let dragKey = null;

    function saveColumnOrder() {
      try {
        localStorage.setItem(ORDER_STORAGE_KEY, JSON.stringify(columnOrder));
      } catch {
        // 사생활 모드 등 저장 실패는 무시하고 기본 동작을 유지한다.
      }
    }

    function isDefaultOrder() {
      const order = defaultOrder();
      return columnOrder.length === order.length
        && columnOrder.every((key, index) => key === order[index]);
    }

    function orderedColumns() {
      return columnOrder.map(key => columnsByKey.get(key)).filter(Boolean);
    }

    function visibleColumns() {
      return orderedColumns().filter(column => visible.has(column.key));
    }

    function visibleKeys() {
      return visibleColumns().map(column => column.key);
    }

    function saveVisibleColumns() {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(visibleKeys()));
      } catch {
        // 사생활 모드 등 저장 실패는 무시하고 기본 동작을 유지한다.
      }
    }

    function normalizeSearchText(value) {
      return String(value ?? "").normalize("NFC").toLowerCase();
    }

    function searchKeywordsFor(query) {
      const text = String(query ?? "");
      if (!text.trim()) return [];
      return text.trim().split(/\\s+/).map(part => normalizeSearchText(part)).filter(part => part.length > 0);
    }

    function eventTsText(iso) {
      if (!iso || typeof iso !== "string") return "";
      return iso.slice(0, 16).replace("T", " ");
    }

    function eventSearchableText(event) {
      if (!event || typeof event !== "object") return "";
      const fields = [event.task_id, event.title, event.bot, event.stage, event.note, event.next, eventTsText(event.ts)];
      return fields.map(value => typeof value === "string" ? value : value == null ? "" : String(value)).join(" ");
    }

    function searchEventMatches(event, keywords) {
      if (!Array.isArray(keywords) || !keywords.length) return false;
      const normalizedKeywords = keywords.map(keyword => normalizeSearchText(keyword)).filter(keyword => keyword.length > 0);
      if (!normalizedKeywords.length) return false;
      const haystack = normalizeSearchText(eventSearchableText(event));
      return normalizedKeywords.every(keyword => haystack.includes(keyword));
    }

    function matchingEventsForTask(task, keywords) {
      if (!task || !Array.isArray(task.events) || !Array.isArray(keywords) || !keywords.length) return [];
      return task.events.filter(event => searchEventMatches(event, keywords));
    }

    function appendHighlightedText(parent, text, keywords) {
      const original = String(text ?? "");
      if (!parent) return;
      const normalizedKeywords = Array.isArray(keywords)
        ? keywords.map(keyword => normalizeSearchText(keyword)).filter(keyword => keyword.length > 0)
        : [];
      if (!normalizedKeywords.length || !original) {
        if (original) parent.append(document.createTextNode(original));
        return;
      }
      const haystack = normalizeSearchText(original);
      const intervals = [];
      for (const keyword of normalizedKeywords) {
        let from = 0;
        while (true) {
          const index = haystack.indexOf(keyword, from);
          if (index === -1) break;
          intervals.push([index, index + keyword.length]);
          from = index + 1;
        }
      }
      if (!intervals.length) {
        parent.append(document.createTextNode(original));
        return;
      }
      intervals.sort((a, b) => (a[0] - b[0]) || (a[1] - b[1]));
      const merged = [];
      for (const pair of intervals) {
        const start = pair[0];
        const end = pair[1];
        const last = merged[merged.length - 1];
        if (last && start < last[1]) {
          if (end > last[1]) last[1] = end;
        } else {
          merged.push([start, end]);
        }
      }
      let cursor = 0;
      for (const pair of merged) {
        const start = Math.min(pair[0], original.length);
        const end = Math.min(pair[1], original.length);
        if (start > cursor) parent.append(document.createTextNode(original.slice(cursor, start)));
        if (end > start) {
          const mark = document.createElement("mark");
          mark.append(document.createTextNode(original.slice(start, end)));
          parent.append(mark);
        }
        cursor = Math.max(cursor, end);
      }
      if (cursor < original.length) parent.append(document.createTextNode(original.slice(cursor)));
    }

    function element(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function setTime(node, iso) {
      // API 시각은 모두 +09:00이므로 브라우저의 시간대와 무관하다.
      if (!iso || typeof iso !== "string") {
        node.removeAttribute("datetime");
        node.textContent = "—";
        return;
      }
      node.dateTime = iso;
      node.textContent = iso.slice(0, 16).replace("T", " ");
    }

    function timeCell(iso, keywords) {
      const cell = element("td");
      const time = element("time");
      if (!iso || typeof iso !== "string") {
        time.removeAttribute("datetime");
        appendHighlightedText(time, "—", keywords);
      } else {
        time.dateTime = iso;
        appendHighlightedText(time, iso.slice(0, 16).replace("T", " "), keywords);
      }
      cell.append(time);
      return cell;
    }

    function stageCell(stage, keywords) {
      const styles = {접수: "intake", 완료: "done", 막힘: "blocked", 계획: "check", 리뷰: "check", 점검: "check"};
      const cell = element("td");
      const badge = element("span", "badge " + (styles[stage] || ""));
      const inner = element("span");
      appendHighlightedText(inner, stage ?? "", keywords);
      badge.append(inner);
      cell.append(badge);
      return cell;
    }

    function textCell(value, keywords) {
      const cell = element("td", "free-text");
      const inner = element("span");
      appendHighlightedText(inner, value || "—", keywords);
      cell.append(inner);
      return cell;
    }

    function historySummary(task, keywords) {
      const section = element("section", "history-summary");
      section.setAttribute("aria-label", "작업 요약");
      const idNode = element("strong", "summary-id");
      appendHighlightedText(idNode, task.task_id ?? "", keywords);
      section.append(idNode);
      if (task.title) {
        const titleNode = element("span", "summary-title");
        appendHighlightedText(titleNode, task.title, keywords);
        section.append(titleNode);
      }
      const dl = element("dl");
      dl.append(element("dt", "", "봇"));
      const botDd = element("dd");
      appendHighlightedText(botDd, botName(task), keywords);
      dl.append(botDd);
      dl.append(element("dt", "", "현재 단계"));
      const stageDd = element("dd");
      stageDd.append(stageCell(task.stage, keywords).querySelector(".badge"));
      dl.append(stageDd);
      dl.append(element("dt", "", "상태"));
      const statusDd = element("dd");
      appendHighlightedText(statusDd, taskStatus(task), keywords);
      dl.append(statusDd);
      dl.append(element("dt", "", "시작"));
      const startedDd = element("dd");
      startedDd.append(timeCell(task.started, keywords).querySelector("time"));
      dl.append(startedDd);
      dl.append(element("dt", "", "마지막 업데이트"));
      const updatedDd = element("dd");
      updatedDd.append(timeCell(task.updated, keywords).querySelector("time"));
      dl.append(updatedDd);
      dl.append(element("dt", "", "이벤트 수"));
      const countDd = element("dd");
      const eventCount = Array.isArray(task.events) ? task.events.length : 0;
      appendHighlightedText(countDd, `${eventCount}개`, keywords);
      dl.append(countDd);
      if (task.note) {
        dl.append(element("dt", "", "메모"));
        const noteDd = element("dd", "summary-wide");
        appendHighlightedText(noteDd, task.note, keywords);
        dl.append(noteDd);
      }
      if (task.next) {
        dl.append(element("dt", "", "다음 할 일"));
        const nextDd = element("dd", "summary-wide");
        appendHighlightedText(nextDd, task.next, keywords);
        dl.append(nextDd);
      }
      section.append(dl);
      return section;
    }

    function historyRow(task, id, options) {
      const row = element("tr", "history");
      row.id = id;
      const cell = element("td");
      cell.colSpan = visible.size;
      const events = options && Array.isArray(options.events) ? options.events : (task && Array.isArray(task.events) ? task.events : []);
      const heading = options && typeof options.heading === "string" ? options.heading : "이벤트 이력";
      const keywords = options && Array.isArray(options.keywords) ? options.keywords : null;
      const hitSet = options && options.hitSet instanceof Set ? options.hitSet : null;
      const table = element("table");
      table.setAttribute("aria-label", "이벤트 이력");
      const header = table.createTHead().insertRow();
      for (const label of ["시각", "단계", "메모", "다음 할 일"]) {
        const th = element("th", "", label);
        th.scope = "col";
        header.append(th);
      }
      const body = table.createTBody();
      for (const event of events) {
        const isHit = hitSet ? hitSet.has(event) : Boolean(keywords && keywords.length && searchEventMatches(event, keywords));
        const activeKeywords = isHit ? keywords : null;
        const eventRow = body.insertRow();
        if (isHit && activeKeywords && activeKeywords.length) eventRow.classList.add("hit");
        const time = timeCell(event.ts, activeKeywords); time.dataset.label = "시각";
        const stage = stageCell(event.stage, activeKeywords); stage.dataset.label = "단계";
        const note = textCell(event.note, activeKeywords); note.dataset.label = "메모";
        const next = textCell(event.next, activeKeywords); next.dataset.label = "다음 할 일";
        eventRow.append(time, stage, note, next);
      }
      cell.append(element("h2", "", heading), historySummary(task, keywords), table);
      row.append(cell);
      return row;
    }

    function emptyRow(message) {
      const row = element("tr");
      const cell = element("td", "empty", message);
      cell.colSpan = visible.size;
      row.append(cell);
      return row;
    }

    function botName(task) {
      const name = typeof task.bot === "string" ? task.bot.trim() : "";
      return name || "(미지정)";
    }

    const STATUS_RANK = {"진행 중": 0, "막힘": 1, "완료": 2};

    function taskStatus(task) {
      // status가 있으면 그 값을 쓰고, 없으면 stage에서 유도한다.
      // 접수부터 점검까지의 단계는 모두 진행 중이다.
      if (task && typeof task.status === "string" && task.status) return task.status;
      if (task && task.stage === "완료") return "완료";
      if (task && task.stage === "막힘") return "막힘";
      return "진행 중";
    }

    function taskStatusRank(task) {
      return STATUS_RANK[taskStatus(task)] ?? 0;
    }

    function isCompletedTask(task) {
      return taskStatus(task) === "완료";
    }

    function defaultTaskCompare(a, b) {
      const group = taskStatusRank(a) - taskStatusRank(b);
      if (group !== 0) return group;
      const byId = String(a.task_id ?? "").localeCompare(
        String(b.task_id ?? ""), "ko", {numeric: true, sensitivity: "base"});
      if (byId !== 0) return byId;
      return String(b.updated || "").localeCompare(String(a.updated || ""));
    }

    function sortTasks(list, compare) {
      // 기본 비교기는 서버 기본 순서와 같다(상태 그룹, task_id 자연 정렬,
      // task_id 동률은 마지막 업데이트 내림차순).
      // 커스텀 비교기가 와도 상태 그룹 파티션(진행 중, 막힘, 완료)은 항상
      // 마지막에 안정 파티션으로 적용하므로 완료는 뒤에 남는다.
      const ordered = compare ? [...list].sort(compare) : [...list].sort(defaultTaskCompare);
      return [
        ...ordered.filter(task => taskStatusRank(task) === 0),
        ...ordered.filter(task => taskStatusRank(task) === 1),
        ...ordered.filter(task => taskStatusRank(task) === 2),
      ];
    }

    function updateBotOptions() {
      const previous = botFilter.value;
      const names = new Set(tasks.map(botName));
      botFilter.replaceChildren(botFilter.firstElementChild);
      for (const name of [...names].sort((a, b) => a.localeCompare(b, "ko"))) {
        const option = element("option", "", name);
        option.value = name;
        botFilter.append(option);
      }
      botFilter.value = names.has(previous) ? previous : "";
    }

    function focusHeaderCell(key) {
      const th = headRow.querySelector(`th[data-column="${key}"]`);
      if (th) th.focus({preventScroll: true});
    }

    function clearDropTargets() {
      headRow.querySelectorAll(".drop-target").forEach(node => node.classList.remove("drop-target"));
    }

    function swapColumnOrder(firstKey, secondKey) {
      const first = columnOrder.indexOf(firstKey);
      const second = columnOrder.indexOf(secondKey);
      if (first < 0 || second < 0 || first === second) return;
      [columnOrder[first], columnOrder[second]] = [columnOrder[second], columnOrder[first]];
    }

    function moveColumnTo(sourceKey, targetKey) {
      if (columnOrder.indexOf(sourceKey) < 0 || columnOrder.indexOf(targetKey) < 0) return;
      if (sourceKey === targetKey) return;
      const [moved] = columnOrder.splice(columnOrder.indexOf(sourceKey), 1);
      columnOrder.splice(columnOrder.indexOf(targetKey), 0, moved);
    }

    function refreshColumnLayout() {
      applyColumnsToHeader();
      buildColumnPanel();
      render();
    }

    function applyColumnsToHeader() {
      colgroup.replaceChildren();
      headRow.replaceChildren();
      for (const column of visibleColumns()) {
        const col = element("col", column.colClass);
        colgroup.append(col);
        const th = element("th", "", column.label);
        th.scope = "col";
        th.dataset.column = column.key;
        th.draggable = true;
        th.tabIndex = 0;
        th.setAttribute("aria-label", column.label);
        th.setAttribute("aria-description", "드래그하거나 Alt+왼쪽/오른쪽 화살표 키로 열 순서를 이동합니다.");
        if (column.ariaSort) th.setAttribute("aria-sort", column.ariaSort);
        th.addEventListener("keydown", event => {
          if (!event.altKey) return;
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
          event.preventDefault();
          const delta = event.key === "ArrowLeft" ? -1 : 1;
          const columns = visibleColumns();
          const index = columns.findIndex(entry => entry.key === column.key);
          const neighbor = columns[index + delta];
          if (!neighbor) return;
          swapColumnOrder(column.key, neighbor.key);
          saveColumnOrder();
          refreshColumnLayout();
          focusHeaderCell(column.key);
        });
        th.addEventListener("dragstart", event => {
          dragKey = column.key;
          th.classList.add("dragging");
          if (event.dataTransfer) {
            try {
              event.dataTransfer.effectAllowed = "move";
              event.dataTransfer.setData("text/plain", column.key);
            } catch {
              // 합성 이벤트 등 dataTransfer가 없는 환경에서는 dragKey로 이어간다.
            }
          }
        });
        th.addEventListener("dragover", event => {
          event.preventDefault();
          if (event.dataTransfer) {
            try {
              event.dataTransfer.dropEffect = "move";
            } catch {
              // dropEffect 설정 실패는 드롭 자체를 막지 않는다.
            }
          }
          th.classList.add("drop-target");
        });
        th.addEventListener("dragleave", () => clearDropTargets());
        th.addEventListener("drop", event => {
          event.preventDefault();
          let sourceKey = dragKey;
          if (event.dataTransfer) {
            try {
              const value = event.dataTransfer.getData("text/plain");
              if (value) sourceKey = value;
            } catch {
              // getData를 읽을 수 없으면 dragstart에서 기록한 키를 쓴다.
            }
          }
          clearDropTargets();
          dragKey = null;
          if (!sourceKey || sourceKey === column.key) return;
          moveColumnTo(sourceKey, column.key);
          saveColumnOrder();
          refreshColumnLayout();
          focusHeaderCell(sourceKey);
        });
        // dragend는 드래그 소스 th에서만 발생하므로 헤더 행 전체의
        // .drop-target을 제거해 대상 th에 남지 않게 한다.
        th.addEventListener("dragend", () => {
          th.classList.remove("dragging");
          clearDropTargets();
          dragKey = null;
        });
        headRow.append(th);
      }
      const total = visibleColumns().reduce((sum, column) => sum + (column.width || 0), 0);
      // 11.6은 기본 최소 너비 1160px를 열 폭 합 100으로 나눈 값이다.
      table.style.minWidth = Math.max(320, Math.round(total * 11.6)) + "px";
      for (const cell of rows.querySelectorAll("td.empty")) {
        cell.colSpan = visible.size;
      }
    }

    function updatePanelDisabled() {
      for (const input of columnsPanel.querySelectorAll("input")) {
        input.disabled = visible.size === 1 && input.checked;
      }
    }

    function movePanelColumn(key, delta) {
      const index = columnOrder.indexOf(key);
      const target = index + delta;
      if (index < 0 || target < 0 || target >= columnOrder.length) return;
      [columnOrder[index], columnOrder[target]] = [columnOrder[target], columnOrder[index]];
      saveColumnOrder();
      refreshColumnLayout();
      const item = columnsPanel.querySelector(`.column-item[data-column="${key}"]`);
      if (!item) return;
      const direction = delta < 0 ? "up" : "down";
      const pressed = item.querySelector(`button[data-direction="${direction}"]`);
      if (pressed && !pressed.disabled) {
        pressed.focus({preventScroll: true});
        return;
      }
      const opposite = item.querySelector(`button[data-direction="${direction === "up" ? "down" : "up"}"]`);
      if (opposite && !opposite.disabled) opposite.focus({preventScroll: true});
    }

    function buildColumnPanel() {
      columnsPanel.replaceChildren();
      const ordered = orderedColumns();
      ordered.forEach((column, index) => {
        const item = element("div", "column-item");
        item.dataset.column = column.key;
        const label = element("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = visible.has(column.key);
        input.dataset.column = column.key;
        label.append(input, element("span", "", column.label));
        const up = document.createElement("button");
        up.type = "button";
        up.className = "column-move";
        up.dataset.direction = "up";
        up.textContent = "▲";
        up.setAttribute("aria-label", column.label + " 위로 이동");
        up.disabled = index === 0;
        up.addEventListener("click", event => {
          event.stopPropagation();
          movePanelColumn(column.key, -1);
        });
        const down = document.createElement("button");
        down.type = "button";
        down.className = "column-move";
        down.dataset.direction = "down";
        down.textContent = "▼";
        down.setAttribute("aria-label", column.label + " 아래로 이동");
        down.disabled = index === ordered.length - 1;
        down.addEventListener("click", event => {
          event.stopPropagation();
          movePanelColumn(column.key, 1);
        });
        item.append(label, up, down);
        columnsPanel.append(item);
      });
      const reset = element("button", "reset-order", "기본 순서로");
      reset.type = "button";
      reset.disabled = isDefaultOrder();
      reset.addEventListener("click", event => {
        event.stopPropagation();
        columnOrder = defaultOrder();
        saveColumnOrder();
        refreshColumnLayout();
      });
      columnsPanel.append(reset);
      updatePanelDisabled();
    }

    function isPanelOpen() {
      return columnsButton.getAttribute("aria-expanded") === "true";
    }

    function setPanelOpen(open) {
      columnsButton.setAttribute("aria-expanded", String(open));
      columnsPanel.hidden = !open;
      columnsBackdrop.hidden = !open;
    }

    function readSearchQueryFromUrl() {
      try {
        return new URLSearchParams(location.search).get("q") ?? "";
      } catch {
        return "";
      }
    }

    function updateSearchUrl() {
      try {
        const url = new URL(location.href);
        if (searchQuery && searchQuery.trim()) {
          url.searchParams.set("q", searchQuery);
        } else {
          url.searchParams.delete("q");
        }
        history.replaceState(null, "", url.pathname + url.search + url.hash);
      } catch {
        // 파일 URL 등 replaceState가 막힌 환경에서는 목록 동작만 유지한다.
      }
    }

    function updateSearchClearVisibility() {
      if (!searchInput || !searchClear) return;
      searchClear.hidden = !searchInput.value;
    }

    function applySearchFromInput() {
      searchQuery = searchInput ? searchInput.value : "";
      updateSearchClearVisibility();
      updateSearchUrl();
      render();
    }

    function scheduleSearchApply() {
      if (searchDebounce) clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => {
        searchDebounce = null;
        applySearchFromInput();
      }, 200);
    }

    function clearSearch() {
      if (searchDebounce) {
        clearTimeout(searchDebounce);
        searchDebounce = null;
      }
      if (searchInput) searchInput.value = "";
      searchQuery = "";
      updateSearchClearVisibility();
      updateSearchUrl();
      render();
    }

    function render() {
      const keywords = searchKeywordsFor(searchQuery);
      const searchActive = keywords.length > 0;
      const botTasks = botFilter.value ? tasks.filter(task => botName(task) === botFilter.value) : tasks;
      const searchFiltered = searchActive
        ? botTasks.filter(task => matchingEventsForTask(task, keywords).length > 0)
        : botTasks;
      for (const button of filters) {
        const status = button.dataset.status;
        const count = status === "전체" ? searchFiltered.length : searchFiltered.filter(t => t.status === status).length;
        button.querySelector(".count").textContent = loaded ? count : "—";
        button.setAttribute("aria-pressed", String(status === selected));
      }
      if (!loaded) {
        if (searchStatus) {
          searchStatus.hidden = true;
          searchStatus.textContent = "";
        }
        return;
      }
      const matching = sortTasks(searchFiltered.filter(task => selected === "전체" || task.status === selected));
      if (searchStatus) {
        if (searchActive) {
          let eventTotal = 0;
          for (const task of matching) eventTotal += matchingEventsForTask(task, keywords).length;
          searchStatus.textContent = `검색어 "${searchQuery.trim()}": ${matching.length}개 작업, ${eventTotal}개 이벤트 일치`;
          searchStatus.hidden = false;
        } else {
          searchStatus.textContent = "";
          searchStatus.hidden = true;
        }
      }
      const fragment = document.createDocumentFragment();
      const focusedId = document.activeElement?.dataset.taskId;
      const titleHidden = !visible.has("title");
      let restoreFocus;
      for (const [index, task] of matching.entries()) {
        const expanded = task.task_id === expandedId;
        const row = element("tr", expanded ? "task-row expanded" : "task-row");
        if (titleHidden) {
          row.setAttribute("tabindex", "0");
          row.setAttribute("aria-expanded", String(expanded));
          row.setAttribute("aria-label", task.task_id + " 이력 보기");
          row.dataset.taskId = task.task_id;
          row.addEventListener("keydown", event => {
            if (event.target !== row) return;
            if (event.key === "Enter" || event.key === " " || event.key === "Spacebar" || event.key === "Space") {
              if (event.key !== "Enter") event.preventDefault();
              expandedId = expanded ? null : task.task_id;
              render();
            }
          });
        }
        for (const column of visibleColumns()) {
          const cell = column.cell(task, expanded, keywords);
          cell.dataset.label = column.label;
          cell.dataset.column = column.key;
          row.append(cell);
        }
        const toggle = visible.has("title") ? row.querySelector(".task-toggle") : null;
        row.addEventListener("click", () => {
          expandedId = expanded ? null : task.task_id;
          render();
        });
        fragment.append(row);
        if (expanded) {
          const id = "history-" + index;
          if (toggle) toggle.setAttribute("aria-controls", id);
          if (titleHidden) row.setAttribute("aria-controls", id);
          if (searchActive) {
            const matched = matchingEventsForTask(task, keywords);
            fragment.append(historyRow(task, id, {
              keywords: keywords,
              hitSet: new Set(matched),
            }));
          } else {
            fragment.append(historyRow(task, id));
          }
        } else if (searchActive) {
          const matched = matchingEventsForTask(task, keywords);
          if (matched.length) {
            const id = "history-" + index;
            if (toggle) toggle.setAttribute("aria-controls", id);
            if (titleHidden) row.setAttribute("aria-controls", id);
            const total = Array.isArray(task.events) ? task.events.length : 0;
            fragment.append(historyRow(task, id, {
              events: matched,
              heading: `일치한 이벤트 ${matched.length}개 (전체 ${total}개)`,
              keywords: keywords,
              hitSet: new Set(matched),
            }));
          }
        }
        if (task.task_id === focusedId) restoreFocus = titleHidden ? row : toggle;
      }
      if (!matching.length) {
        fragment.append(emptyRow(searchActive ? "일치하는 이벤트가 없습니다." : (selected === "전체" ? "등록된 작업이 없습니다." : "이 상태의 작업이 없습니다.")));
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

    botFilter.addEventListener("change", render);

    if (searchInput) {
      searchInput.addEventListener("input", () => {
        updateSearchClearVisibility();
        scheduleSearchApply();
      });
      searchInput.addEventListener("keydown", event => {
        if (event.key === "Enter") {
          event.preventDefault();
          if (searchDebounce) {
            clearTimeout(searchDebounce);
            searchDebounce = null;
          }
          applySearchFromInput();
        } else if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          clearSearch();
        }
      });
    }
    if (searchClear) {
      searchClear.addEventListener("click", () => {
        clearSearch();
      });
    }
    window.addEventListener("popstate", () => {
      const nextQuery = readSearchQueryFromUrl();
      if (searchInput && searchInput.value !== nextQuery) searchInput.value = nextQuery;
      searchQuery = nextQuery;
      updateSearchClearVisibility();
      render();
    });

    async function refresh() {
      if (refreshButton.disabled) return;
      refreshButton.disabled = true;
      try {
        const response = await fetch("/api/tasks", {cache: "no-store"});
        if (!response.ok) throw new Error("목록 조회 실패");
        const data = await response.json();
        tasks = data.tasks;
        updateBotOptions();
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

    columnsButton.addEventListener("click", () => {
      setPanelOpen(!isPanelOpen());
    });

    columnsBackdrop.addEventListener("click", () => {
      setPanelOpen(false);
    });

    columnsPanel.addEventListener("change", event => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || !input.dataset.column) return;
      const key = input.dataset.column;
      if (input.checked) {
        visible.add(key);
      } else {
        if (visible.size <= 1) {
          input.checked = true;
          return;
        }
        visible.delete(key);
      }
      saveVisibleColumns();
      applyColumnsToHeader();
      updatePanelDisabled();
      render();
    });

    document.addEventListener("click", event => {
      if (!isPanelOpen()) return;
      const target = event.target;
      if (target instanceof Node && columnsButton.contains(target)) return;
      if (target instanceof Node && columnsPanel.contains(target)) return;
      setPanelOpen(false);
    });

    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && isPanelOpen()) {
        setPanelOpen(false);
        columnsButton.focus();
      }
    });

    buildColumnPanel();
    applyColumnsToHeader();

    searchQuery = readSearchQueryFromUrl();
    if (searchInput) searchInput.value = searchQuery;
    updateSearchClearVisibility();

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
        print(f"봇 작업 대시보드가 {server.server_address[1]} 포트에서 실행 중입니다.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
