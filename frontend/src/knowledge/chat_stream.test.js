import assert from "node:assert/strict";
import test from "node:test";
import { readAnswerStream } from "../api.js";

function response(text, size = 1) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({
    start(controller) {
      for (let i = 0; i < bytes.length; i += size) controller.enqueue(bytes.slice(i, i + size));
      controller.close();
    },
  }));
}

test("SSE tolerates fragmented Chinese bytes, CRLF and heartbeat", async () => {
  const events = [];
  const result = await readAnswerStream(response(
    ': heartbeat\r\n\r\nevent: stage\r\ndata: {"text":"正在检索"}\r\n\r\n' +
    'event: final\ndata: {"answer":"已核对的答案","sources":[]}\n\n',
  ), (event) => events.push(event));
  assert.deepEqual(events, [{ text: "正在检索" }]);
  assert.equal(result.answer, "已核对的答案");
});

test("progress arrives before final answer exists", async () => {
  let controller;
  const events = [];
  const stream = new ReadableStream({ start(value) { controller = value; } });
  const pending = readAnswerStream(new Response(stream), (event) => events.push(event));
  const encoder = new TextEncoder();
  controller.enqueue(encoder.encode('event: stage\ndata: {"text":"整理答案"}\n\n'));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(events.length, 1);
  controller.enqueue(encoder.encode('event: final\ndata: {"answer":"完成"}\n\n'));
  assert.equal((await pending).answer, "完成");
});

test("incomplete / failed streams never masquerade as a completed answer", async () => {
  await assert.rejects(readAnswerStream(response('event: stage\ndata: {"text":"检索"}\n\n')), /连接已断开/);
  await assert.rejects(readAnswerStream(response('event: error\ndata: {"detail":"模型不可用"}\n\n')), /模型不可用/);
  await assert.rejects(readAnswerStream(response('event: final\ndata: {"answer":')), /连接已断开/);
});
