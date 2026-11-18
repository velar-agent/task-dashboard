import assert from "node:assert/strict";
import test from "node:test";
import page from "../src/html.js";
import worker, {handleRequest, TaskStore} from "../src/index.js";

const origin = "https://dashboard.test";
const secret = "test-only-token";
const limit = 1024 * 1024;

function fixture() {
  const values = new Map();
  const storage = {
    get: key => values.get(key),
    put: (key, value) => values.set(key, structuredClone(value)),
  };
  const env = {
    INGEST_KEY: secret,
    STORE: {
      idFromName(name) {
        assert.equal(name, "main");
        return "main-id";
      },
      get(id) {
        assert.equal(id, "main-id");
        return new TaskStore({storage});
      },
    },
  };
  const request = (path, init) => handleRequest(new Request(origin + path, init), env);
  const put = (body, headers = {}, init = {}) => request("/api/ingest", {
    method: "PUT", body, headers: {Authorization: "Bearer " + secret, ...headers}, ...init,
  });
  return {request, put, values, env};
}

function checkHeaders(response, type) {
  assert.equal(response.headers.get("Content-Type"), type + "; charset=utf-8");
  assert.equal(response.headers.get("Cache-Control"), "no-store");
  assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
}

async function readJson(response, status) {
  assert.equal(response.status, status);
  checkHeaders(response, "application/json");
  return response.json();
}

test("the page and health check work before storage or a secret is configured", async () => {
  const response = await worker.fetch(new Request(origin + "/?refresh=1"), {});
  assert.equal(response.status, 200);
  checkHeaders(response, "text/html");
  assert.equal(await response.text(), page);
  const health = await handleRequest(new Request(origin + "/healthz"), {});
  assert.deepEqual(await readJson(health, 200), {status: "ok"});
});

test("empty snapshot and routing errors", async () => {
  const {request} = fixture();
  assert.deepEqual(await readJson(await request("/api/tasks"), 200), {
    tasks: [], generated_at: null, pushed_at: null,
  });
  for (const id of ["missing", "%C3"]) {
    assert.deepEqual(await readJson(await request("/api/tasks/" + id), 404), {error: "작업을 찾을 수 없습니다."});
  }
  assert.deepEqual(await readJson(await request("/missing"), 404), {error: "경로를 찾을 수 없습니다."});
  for (const [path, method, allow] of [
    ["/", "POST", "GET"], ["/healthz", "DELETE", "GET"], ["/api/tasks", "PUT", "GET"],
    ["/api/tasks/1", "POST", "GET"], ["/api/ingest", "GET", "PUT"], ["/api/ingest", "POST", "PUT"],
  ]) {
    const response = await request(path, {method});
    assert.equal(response.headers.get("Allow"), allow);
    assert.ok((await readJson(response, 405)).error);
  }
});

test("a push replaces the single stored snapshot and preserves folded task details", async () => {
  const {request, put, values} = fixture();
  const task = {
    task_id: "작업/1 ?+#", bot: "운영", title: "<em>점검</em>", stage: "완료",
    events: [{stage: "계획"}, {stage: "완료", note: "확인"}],
  };
  const snapshot = {tasks: [task, {task_id: "second"}], generated_at: "2026-11-18T12:00:00+09:00"};
  const before = Date.now();
  const pushed = await put(JSON.stringify({...snapshot, pushed_at: "client time"}));
  assert.deepEqual(await readJson(pushed, 200), {ok: true, count: 2});
  assert.deepEqual([...values.keys()], ["snapshot"]);
  const list = await readJson(await request("/api/tasks?refresh=1"), 200);
  assert.match(list.pushed_at, /\+09:00$/);
  assert.ok(Date.parse(list.pushed_at) >= before && Date.parse(list.pushed_at) <= Date.now());
  assert.deepEqual(list, {...snapshot, pushed_at: list.pushed_at});
  assert.deepEqual(await readJson(await request("/api/tasks/" + encodeURIComponent(task.task_id) + "?v=1"), 200), task);

  assert.deepEqual(await readJson(await put('{"tasks":[]}'), 200), {ok: true, count: 0});
  const replaced = await readJson(await request("/api/tasks"), 200);
  assert.deepEqual(replaced.tasks, []);
  assert.equal(replaced.generated_at, null);
  assert.deepEqual(await readJson(await request("/api/tasks/second"), 404), {error: "작업을 찾을 수 없습니다."});
});

test("ingest checks the configured Bearer secret before processing the body", async () => {
  const {request, put, values, env} = fixture();
  const body = "broken JSON";
  const responses = [await request("/api/ingest", {method: "PUT", body})];
  for (const token of ["Basic " + secret, "Bearer", "Bearer test-only-tokem", "Bearer short"]) {
    responses.push(await put(body, {Authorization: token}));
  }
  for (const response of responses) {
    assert.equal(response.headers.get("WWW-Authenticate"), "Bearer");
    assert.ok((await readJson(response, 401)).error);
  }
  delete env.INGEST_KEY;
  assert.ok((await readJson(await put(body), 503)).error);
  assert.equal(values.size, 0);
});

test("bad UTF-8, JSON and task schemas leave the previous snapshot intact", async () => {
  const {request, put} = fixture();
  await readJson(await put('{"tasks":[{"task_id":"keep"}]}'), 200);
  const original = await readJson(await request("/api/tasks"), 200);
  for (const body of [
    undefined, "", "{", "null", "[]", "42", "{}", '{"tasks":{}}', '{"tasks":[null]}',
    '{"tasks":[42]}', '{"tasks":[[]]}', '{"tasks":[{}]}', '{"tasks":[{"task_id":1}]}',
    new Uint8Array([123, 34, 0xff, 34, 58, 48, 125]),
  ]) {
    assert.ok((await readJson(await put(body), 400)).error);
  }
  assert.deepEqual(await readJson(await request("/api/tasks"), 200), original);
});

test("the byte limit is inclusive and enforced on the stream with or without Content-Length", async () => {
  const {put, request} = fixture();
  const source = '{"tasks":[{"task_id":"한"}]}';
  const exact = source.padEnd(source.length + limit - Buffer.byteLength(source));
  const bytes = new TextEncoder().encode(exact);
  function stream(parts) {
    return new ReadableStream({start(controller) {
      for (const part of parts) controller.enqueue(part);
      controller.close();
    }});
  }
  const split = Buffer.byteLength(source.split("한")[0]) + 1;
  const accepted = await put(stream([bytes.slice(0, split), bytes.slice(split)]), {}, {duplex: "half"});
  assert.deepEqual(await readJson(accepted, 200), {ok: true, count: 1});
  const original = await readJson(await request("/api/tasks"), 200);
  assert.equal(original.tasks[0].task_id, "한");
  assert.ok((await readJson(await put("{}", {"Content-Length": String(limit + 1)}), 413)).error);
  for (const headers of [{}, {"Content-Length": "1"}]) {
    const oversized = stream([bytes, new Uint8Array([32])]);
    assert.ok((await readJson(await put(oversized, headers, {duplex: "half"}), 413)).error);
  }
  assert.deepEqual(await readJson(await request("/api/tasks"), 200), original);
});
