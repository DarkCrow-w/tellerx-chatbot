/** HTTP adapter for the browser application. */

async function requestJson(path, options = {}) {
  // 统一把 FastAPI 的 detail 转换为界面可以直接展示的错误。
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string"
      ? body.detail
      : Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg).filter(Boolean).join("；")
        : `请求失败（${response.status}）`;
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

export function documentDownloadUrl(documentId, versionId = null) {
  /** 构造浏览器可直接打开的原文件下载地址。 */
  const path = `/api/v1/documents/${encodeURIComponent(documentId)}/download`;
  return versionId ? `${path}?version_id=${encodeURIComponent(versionId)}` : path;
}

export function askKnowledgeBase({ question, conversationId, projectId }) {
  /** 提交一次问答，并把前端命名转换为 API 契约字段。 */
  return requestJson("/api/v1/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      conversation_id: conversationId,
      project_ids: projectId ? [projectId] : [],
    }),
  });
}
