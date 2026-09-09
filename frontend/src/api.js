/** HTTP adapter for the browser application. */

async function requestJson(path, options = {}) {
  // 统一把 FastAPI 的 detail 转换为界面可以直接展示的错误。
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    let detail = `请求失败（${response.status}）`;
    if (typeof body.detail === "string") {
      detail = body.detail;
    } else if (Array.isArray(body.detail)) {
      detail = body.detail.map((item) => item.msg).filter(Boolean).join("；");
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

export function listProjects() {
  /** 获取当前可选的知识库项目。 */
  return requestJson("/api/v1/projects");
}

export function createProject(name) {
  /** 创建一个可在首次上传前存在的空知识库。 */
  return requestJson("/api/v1/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export function renameProject(projectId, name) {
  /** 只修改知识库显示名称，稳定项目 ID 不发生变化。 */
  return requestJson(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export function cleanupProject(projectId) {
  /** 物理回收已软删除文档的残留，不影响仍在使用的文档。 */
  return requestJson(`/api/v1/projects/${encodeURIComponent(projectId)}/cleanup`, {
    method: "POST",
  });
}

export function deleteProject(projectId) {
  /** 彻底删除知识库及其全部文档和派生数据。 */
  return requestJson(`/api/v1/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
}

export function getDocumentCapabilities() {
  /** 获取后端真实支持的格式和单文件体积限制。 */
  return requestJson("/api/v1/documents/capabilities");
}

export function listDocuments({ projectId, query = "", limit = 20, offset = 0 }) {
  /** 分页读取知识库文档及其最新构建状态。 */
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (query.trim()) params.set("q", query.trim());
  return requestJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/documents?${params}`,
  );
}

export function uploadDocument({
  file,
  project,
  documentType,
  logicalKey,
  versionLabel,
  owner,
}) {
  /** 使用 multipart 上传一份不可变原文件，并返回后台入库任务。 */
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("project", project);
  form.append("document_type", documentType);
  form.append("lifecycle_status", "approved");
  form.append("logical_key", logicalKey);
  if (versionLabel?.trim()) form.append("version_label", versionLabel.trim());
  if (owner?.trim()) form.append("owner", owner.trim());
  return requestJson("/api/v1/documents", { method: "POST", body: form });
}

export function getIngestionJob(jobId) {
  /** 查询单个文档构建任务的阶段、进度和诊断信息。 */
  return requestJson(`/api/v1/ingestion-jobs/${encodeURIComponent(jobId)}`);
}

export function retryIngestionJob(jobId) {
  /** 为一个已结束任务创建新的入库尝试。 */
  return requestJson(`/api/v1/ingestion-jobs/${encodeURIComponent(jobId)}/retry`, {
    method: "POST",
  });
}

export function listDocumentVersions(documentId) {
  /** 按时间倒序读取文档的不可变版本。 */
  return requestJson(`/api/v1/documents/${encodeURIComponent(documentId)}/versions`);
}

export function approveDocumentVersion(versionId) {
  /** 将已完成技术构建的草稿版本批准为当前版本。 */
  return requestJson(`/api/v1/document-versions/${encodeURIComponent(versionId)}/approve`, {
    method: "POST",
  });
}

export function deprecateDocumentVersion(versionId) {
  /** 废弃指定版本并从搜索投影移除。 */
  return requestJson(`/api/v1/document-versions/${encodeURIComponent(versionId)}/deprecate`, {
    method: "POST",
  });
}

export function deleteDocument(documentId) {
  /** 软删除逻辑文档及其搜索投影。 */
  return requestJson(`/api/v1/documents/${encodeURIComponent(documentId)}`, {
    method: "DELETE",
  });
}

export function bulkDeleteDocuments(projectId, documentIds) {
  /** 在指定知识库中一次软删除多份逻辑文档。 */
  return requestJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/documents/bulk-delete`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_ids: documentIds }),
    },
  );
}

export function documentDownloadUrl(documentId, versionId = null) {
  /** 构造浏览器可直接打开的原文件下载地址。 */
  const path = `/api/v1/documents/${encodeURIComponent(documentId)}/download`;
  return versionId ? `${path}?version_id=${encodeURIComponent(versionId)}` : path;
}

export function getSectionContext(sectionId) {
  /** 按需加载引用所在章节及其前后相邻块。 */
  return requestJson(`/api/v1/sections/${encodeURIComponent(sectionId)}`);
}

export function askKnowledgeBase({
  question,
  conversationId,
  projectId,
  documentId = null,
  documentHint = null,
  sectionPath = [],
}) {
  /** 提交一次问答，并把前端命名转换为 API 契约字段。 */
  return requestJson("/api/v1/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      conversation_id: conversationId,
      project_ids: projectId ? [projectId] : [],
      document_id: documentId,
      document_hint: documentHint,
      section_path: sectionPath,
    }),
  });
}

/** Read SSE frames across arbitrary network / UTF-8 boundaries. */
export async function readAnswerStream(response, onProgress = () => {}) {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `请求失败（${response.status}）`);
  }
  if (!response.body) throw new Error("浏览器未能建立响应流，请重试。");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const lines = frame.split(/\r?\n/);
        const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
        const data = lines.filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart()).join("\n");
        if (!data) continue;
        const payload = JSON.parse(data);
        if (event === "stage") onProgress(payload);
        if (event === "error") throw new Error(payload.detail || "问答中断，请重试。");
        if (event === "final") return payload;
      }
      if (done) throw new Error("连接已断开，尚未收到完整答案，请重试。");
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export async function askKnowledgeBaseStream({
  question, conversationId, projectId, documentId = null, signal, onProgress,
}) {
  const response = await fetch("/api/v1/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    signal,
    body: JSON.stringify({
      question, conversation_id: conversationId, project_ids: projectId ? [projectId] : [],
      document_id: documentId,
    }),
  });
  return readAnswerStream(response, onProgress);
}

/** Resolve the cited chunk's version; never silently download a newer upload. */
export async function downloadEvidenceDocument(chunkId) {
  const source = await requestJson(`/api/v1/sources/${encodeURIComponent(chunkId)}`);
  const response = await fetch(documentDownloadUrl(source.document_id, source.version_id));
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `请求失败（${response.status}）`);
  }
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = source.filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
