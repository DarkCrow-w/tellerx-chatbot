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
