/** HTTP adapter for the browser application. */

async function requestJson(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : `请求失败（${response.status}）`;
    throw new Error(detail);
  }
  return response.json();
}

export function listProjects() {
  return requestJson("/api/v1/projects");
}

export function askKnowledgeBase({ question, conversationId, projectId }) {
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

