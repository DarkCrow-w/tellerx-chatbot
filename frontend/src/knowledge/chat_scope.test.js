import assert from "node:assert/strict";
import test from "node:test";

import { askKnowledgeBase, getSectionContext } from "../api.js";

async function captureRequest(action) {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, options = {}) => {
    request = { path, options };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  try {
    await action();
    return request;
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("候选选择使用文档 ID 和原问题重新请求", { concurrency: false }, async () => {
  const request = await captureRequest(() => askKnowledgeBase({
    question: "接口失败怎么处理？",
    conversationId: "conversation-1",
    projectId: "project-1",
    documentId: "document-1",
    sectionPath: ["接口模块"],
  }));
  const body = JSON.parse(request.options.body);
  assert.equal(request.path, "/api/v1/chat");
  assert.equal(body.document_id, "document-1");
  assert.deepEqual(body.project_ids, ["project-1"]);
  assert.deepEqual(body.section_path, ["接口模块"]);
});

test("未选项目时发送空项目列表以执行全库检索", { concurrency: false }, async () => {
  const request = await captureRequest(() => askKnowledgeBase({
    question: "鉴权失败怎么处理？",
    conversationId: null,
    projectId: "",
  }));
  assert.deepEqual(JSON.parse(request.options.body).project_ids, []);
});

test("章节上下文路径会安全编码章节 ID", { concurrency: false }, async () => {
  const request = await captureRequest(() => getSectionContext("section/1"));
  assert.equal(request.path, "/api/v1/sections/section%2F1");
});
