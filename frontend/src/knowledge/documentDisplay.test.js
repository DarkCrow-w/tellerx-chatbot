import assert from "node:assert/strict";
import test from "node:test";
import { documentState, displayWarning } from "./documentDisplay.js";

test("废弃版本优先于旧任务的成功状态", () => {
  assert.equal(documentState({
    latest_version: { lifecycle_status: "deprecated", technical_status: "searchable" },
    latest_job: { status: "succeeded" },
  }).key, "deprecated");
});

test("新任务失败时显示失败，不沿用旧版本的可检索状态", () => {
  const state = documentState({
    latest_version: { technical_status: "searchable" },
    latest_job: { status: "failed", progress: 35 },
  });
  assert.deepEqual(state, { key: "failed", label: "构建失败", progress: 35 });
});

test("索引未发布前保持处理中，完成后才显示可检索", () => {
  assert.deepEqual(documentState({ latest_job: { status: "index_pending", stage: "indexing", progress: 75 } }),
    { key: "working", label: "建立索引", progress: 75 });
  assert.equal(documentState({ latest_job: { status: "succeeded" } }).key, "ready");
});

test("仅关键词检索的警告仍有用户可读的说明", () => {
  assert.equal(displayWarning("BM25 only: embedding unavailable"), "向量模型不可用，当前文档仅使用关键词检索");
});
