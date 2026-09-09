import assert from "node:assert/strict";
import test from "node:test";

import { cleanupProject, deleteProject } from "../api.js";

async function captureRequest(action) {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (path, options) => {
    request = { path, options };
    return {
      ok: true,
      status: 200,
      json: async () => ({ project_id: "project/id", project_deleted: false }),
    };
  };
  try {
    const result = await action();
    return { request, result };
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("清理残留调用项目 cleanup 接口", { concurrency: false }, async () => {
  const { request, result } = await captureRequest(() => cleanupProject("project/id"));

  assert.equal(request.path, "/api/v1/projects/project%2Fid/cleanup");
  assert.equal(request.options.method, "POST");
  assert.equal(result.project_deleted, false);
});

test("彻底删除知识库调用项目 DELETE 接口", { concurrency: false }, async () => {
  const { request } = await captureRequest(() => deleteProject("project/id"));

  assert.equal(request.path, "/api/v1/projects/project%2Fid");
  assert.equal(request.options.method, "DELETE");
});

for (const [detail, expected] of [
  ["具体错误", "具体错误"],
  ["", ""],
  [[{ msg: "字段缺失" }, { msg: "" }, { msg: "格式错误" }], "字段缺失；格式错误"],
  [[], ""],
  [null, "请求失败（422）"],
  [{ reason: "invalid" }, "请求失败（422）"],
]) {
  test(`错误详情保留原有文案：${JSON.stringify(detail)}`, async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => ({ ok: false, status: 422, json: async () => ({ detail }) });
    try {
      await assert.rejects(cleanupProject("project"), (error) => error.message === expected);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
}
