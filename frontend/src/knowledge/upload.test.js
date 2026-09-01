import assert from "node:assert/strict";
import test from "node:test";

import {
  friendlyUploadError,
  inferDocumentType,
  logicalKeyFor,
  prepareFiles,
} from "./upload.js";

function fakeFile(name, options = {}) {
  return {
    name,
    size: options.size ?? 100,
    lastModified: options.lastModified ?? 1,
    webkitRelativePath: options.relativePath ?? "",
  };
}

const capabilities = {
  allowed_extensions: [".docx", ".md", ".pdf", ".xlsx"],
  max_upload_bytes: 1024,
};

test("文件夹逻辑键去掉最外层目录并保留内部路径", () => {
  const file = fakeFile("制度.pdf", { relativePath: "本地根目录/财务/制度.pdf" });
  assert.equal(logicalKeyFor(file), "财务/制度.pdf");
});

test("单文件使用文件名，文档类别按后缀推断", () => {
  assert.equal(logicalKeyFor(fakeFile("README.md")), "README.md");
  assert.equal(inferDocumentType("台账.xlsx"), "spreadsheet");
  assert.equal(inferDocumentType("规范.DOCX"), "word-document");
});

test("批量准备会跳过隐藏、未知格式和超限文件，但保留其他文件", () => {
  const entries = prepareFiles([
    fakeFile("有效.pdf", { relativePath: "根/制度/有效.pdf" }),
    fakeFile(".DS_Store", { relativePath: "根/.DS_Store" }),
    fakeFile("脚本.exe", { relativePath: "根/脚本.exe" }),
    fakeFile("超限.docx", { size: 2048, relativePath: "根/超限.docx" }),
  ], capabilities);

  assert.equal(entries[0].status, "queued");
  assert.equal(entries[0].logicalKey, "制度/有效.pdf");
  assert.deepEqual(entries.slice(1).map((entry) => entry.status), ["skipped", "skipped", "skipped"]);
});

test("上传新版本时使用指定逻辑键", () => {
  const [entry] = prepareFiles([fakeFile("新文件名.pdf")], capabilities, "财务/原制度.pdf");
  assert.equal(entry.logicalKey, "财务/原制度.pdf");
});

test("常见入库失败会转换为可操作的中文提示", () => {
  assert.match(friendlyUploadError("No extractable text was found"), /没有可提取/);
  assert.match(friendlyUploadError("ModelAPIError during embedding"), /Embedding 模型/);
});
