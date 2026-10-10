// Run with: npm test (Node's own test runner; no extra packages).
import assert from "node:assert/strict";
import { test } from "node:test";

import { parseSseChunk, readSse, type SseMessage } from "./sse.ts";

test("complete events come out, the unfinished one waits", () => {
  const { messages, rest } = parseSseChunk(
    'event: stage\ndata: {"stage":"triage"}\n\nevent: result\ndata: {"a"',
  );

  assert.deepEqual(messages, [{ event: "stage", data: '{"stage":"triage"}' }]);
  assert.equal(rest, 'event: result\ndata: {"a"');
});

test("CRLF endings, comments and several data lines", () => {
  const { messages } = parseSseChunk(": keep-alive\r\n\r\nevent: x\r\ndata: one\r\ndata: two\r\n\r\n");

  assert.deepEqual(messages, [{ event: "x", data: "one\ntwo" }]);
});

test("an event without a name is a plain message", () => {
  assert.deepEqual(parseSseChunk("data: hi\n\n").messages, [{ event: "message", data: "hi" }]);
});

test("a stream split anywhere gives the same events", async () => {
  const text = 'event: stage\ndata: {"s":1}\n\nevent: result\ndata: {"ok":true}\n\n';
  const bytes = new TextEncoder().encode(text);
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (let i = 0; i < bytes.length; i += 7) controller.enqueue(bytes.slice(i, i + 7));
      controller.close();
    },
  });
  const seen: SseMessage[] = [];

  await readSse(body, (m) => seen.push(m));

  assert.deepEqual(
    seen.map((m) => m.event),
    ["stage", "result"],
  );
  assert.equal(seen[1].data, '{"ok":true}');
});

test("a last event without its blank line still arrives", async () => {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("event: result\ndata: {}"));
      controller.close();
    },
  });
  const seen: SseMessage[] = [];

  await readSse(body, (m) => seen.push(m));

  assert.deepEqual(seen, [{ event: "result", data: "{}" }]);
});
