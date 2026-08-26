/** HTTP adapter for the browser application. */

async function requestJson(path, options = {}) {
  // 统一把 FastAPI 的 detail 转换为界面可以直接展示的错误。
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : `请求失败（${response.status}）`;
    throw new Error(detail);
  }
  return response.json();
}

export function listProjects() {
  /** 获取当前可选的知识库项目。 */
  return requestJson("/api/v1/projects");
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
