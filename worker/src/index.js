import page from "./html.js";

const TASK_PREFIX = "/api/tasks/";
const MAX_BODY_BYTES = 1024 * 1024;
const commonHeaders = {
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
};

function json(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {...commonHeaders, "Content-Type": "application/json; charset=utf-8", ...headers},
  });
}

function methodNotAllowed(allow) {
  return json({error: "허용되지 않은 메서드입니다."}, 405, {Allow: allow});
}

async function authorized(request, secret) {
  const match = /^Bearer +([^\s]+)$/i.exec(request.headers.get("Authorization") || "");
  if (!match) return false;
  const encoder = new TextEncoder();
  const expected = encoder.encode(secret);
  const key = await crypto.subtle.importKey(
    "raw", expected, {name: "HMAC", hash: "SHA-256"}, false, ["sign", "verify"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, expected);
  return crypto.subtle.verify("HMAC", key, signature, encoder.encode(match[1]));
}

function store(env) {
  return env.STORE.get(env.STORE.idFromName("main"));
}

function validTask(task) {
  return task !== null && typeof task === "object" && !Array.isArray(task)
    && typeof task.task_id === "string";
}

function tooLarge() {
  return json({error: "본문이 1 MiB를 초과합니다."}, 413);
}

async function ingest(request, env) {
  if (!env.INGEST_KEY) return json({error: "수집 키가 설정되지 않았습니다."}, 503);
  if (!await authorized(request, env.INGEST_KEY)) {
    return json({error: "인증에 실패했습니다."}, 401, {"WWW-Authenticate": "Bearer"});
  }
  if (Number(request.headers.get("Content-Length")) > MAX_BODY_BYTES) return tooLarge();
  if (!request.body) return json({error: "JSON 본문이 필요합니다."}, 400);

  const reader = request.body.getReader();
  const decoder = new TextDecoder("utf-8", {fatal: true});
  let size = 0;
  let source = "";
  let snapshot;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) {
        await reader.cancel();
        return tooLarge();
      }
      source += decoder.decode(value, {stream: true});
    }
    snapshot = JSON.parse(source + decoder.decode());
  } catch {
    return json({error: "올바른 UTF-8 JSON 본문이 필요합니다."}, 400);
  } finally {
    reader.releaseLock();
  }
  if (!Array.isArray(snapshot?.tasks) || !snapshot.tasks.every(validTask)) {
    return json({error: "tasks 배열의 각 작업에는 문자열 task_id가 필요합니다."}, 400);
  }

  const kst = new Date(Date.now() + 9 * 60 * 60 * 1000);
  snapshot.pushed_at = kst.toISOString().replace("Z", "+09:00");
  snapshot.generated_at ??= null;
  return store(env).fetch(new Request(request.url, {method: "PUT", body: JSON.stringify(snapshot)}));
}

export async function handleRequest(request, env) {
  const path = new URL(request.url).pathname;
  if (path === "/api/ingest") {
    return request.method === "PUT" ? ingest(request, env) : methodNotAllowed("PUT");
  }
  if (path !== "/" && path !== "/healthz" && path !== "/api/tasks" && !path.startsWith(TASK_PREFIX)) {
    return json({error: "경로를 찾을 수 없습니다."}, 404);
  }
  if (request.method !== "GET") return methodNotAllowed("GET");
  if (path === "/") {
    return new Response(page, {headers: {...commonHeaders, "Content-Type": "text/html; charset=utf-8"}});
  }
  if (path === "/healthz") return json({status: "ok"});
  return store(env).fetch(request);
}

export class TaskStore {
  constructor(state) {
    this.storage = state.storage;
  }

  async fetch(request) {
    if (request.method === "PUT") {
      const snapshot = await request.json();
      await this.storage.put("snapshot", snapshot);
      return json({ok: true, count: snapshot.tasks.length});
    }
    if (request.method !== "GET") return methodNotAllowed("GET, PUT");
    const snapshot = await this.storage.get("snapshot") ?? {tasks: [], generated_at: null, pushed_at: null};
    const path = new URL(request.url).pathname;
    if (path === "/api/tasks") return json(snapshot);
    let taskId;
    try {
      taskId = decodeURIComponent(path.slice(TASK_PREFIX.length));
    } catch {
      return json({error: "작업을 찾을 수 없습니다."}, 404);
    }
    const task = snapshot.tasks.find(entry => entry.task_id === taskId);
    return task ? json(task) : json({error: "작업을 찾을 수 없습니다."}, 404);
  }
}

export default {fetch: handleRequest};
