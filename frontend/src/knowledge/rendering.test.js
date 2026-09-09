import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let server;
let Message;
let AnswerProgress;
let SourceCard;
before(async () => {
  server = await createServer({
    configFile: "frontend/vite.config.js",
    server: { middlewareMode: true, watch: null, hmr: false },
  });
  ({ Message } = await server.ssrLoadModule("/src/components.jsx"));
  ({ default: AnswerProgress } = await server.ssrLoadModule("/src/AnswerProgress.jsx"));
  ({ default: SourceCard } = await server.ssrLoadModule("/src/SourceCard.jsx"));
});
after(async () => { await server?.close(); });
const render = (component, props) => renderToStaticMarkup(createElement(component, props));

test("进度标题以运行状态优先，其次失败，最后正常完成", () => {
  for (const [active, failed, title] of [
    [true, true, "正在思考"], [true, false, "正在思考"],
    [false, true, "处理已结束"], [false, false, "处理过程"],
  ]) {
    const html = render(AnswerProgress, { active, failed, startedAt: Date.now() });
    assert.ok(html.includes(title));
    assert.equal(html.includes("停止等待"), active);
  }
});

test("历史消息范围回退与模型信息保持完整", () => {
  const base = { role: "assistant", content: "答案", resolvedScope: "global", sources: [] };
  let html = render(Message, { message: base });
  assert.ok(html.includes("未限定单一文档（历史消息未记录知识库选择）"));
  assert.ok(html.includes("未调用生成模型"));
  html = render(Message, { message: { ...base, searchScope: "知识库：业务", modelId: "model", routeTier: "plus" } });
  assert.ok(html.includes("知识库：业务"));
  assert.ok(html.includes("model · plus"));
  html = render(Message, { message: { ...base, modelId: "model", routeTier: "" } });
  assert.ok(html.includes("<span>model</span>"));
});

test("引用文件去重保留顺序，指定文档优先展示文件名", () => {
  const message = {
    role: "assistant", content: "答案", resolvedScope: "document",
    resolvedDocument: { filename: "指定.md" },
    sources: [{ filename: "乙.md" }, { filename: "甲.md" }, { filename: "乙.md" }],
  };
  const html = render(Message, { message });
  assert.ok(html.includes("检索范围：指定.md"));
  assert.ok(html.includes("实际引用：乙.md、甲.md"));
});

test("证据定位优先使用面包屑，空面包屑回退至标题路径", () => {
  const source = { filename: "业务.md", quote: "原文", breadcrumb: ["业务", "规则"], heading_path: "备用标题", page_number: 2 };
  assert.ok(render(SourceCard, { source }).includes("业务 › 规则 · 第 2 页"));
  assert.ok(render(SourceCard, { source: { ...source, breadcrumb: [] } }).includes("备用标题 · 第 2 页"));
});
