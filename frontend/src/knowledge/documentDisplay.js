export const PAGE_SIZE = 20;
export const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "index_pending"]);

export const STATUS_COPY = {
  queued: "等待处理",
  starting: "准备构建",
  parsing: "解析中",
  embedding: "向量化中",
  indexing: "建立索引",
  succeeded: "可检索",
  failed: "构建失败",
  deprecated: "已废弃",
};

export function displayWarning(warning) {
  if (typeof warning === "string") {
    if (warning.toLowerCase().includes("bm25 only")) {
      return "向量模型不可用，当前文档仅使用关键词检索";
    }
    return warning;
  }
  try {
    return JSON.stringify(warning);
  } catch {
    return "文档处理存在警告";
  }
}

export function documentState(document) {
  const version = document.latest_version;
  const job = document.latest_job;
  if (version?.lifecycle_status === "deprecated") {
    return { key: "deprecated", label: STATUS_COPY.deprecated, progress: 100 };
  }
  if (job?.status === "failed") {
    return { key: "failed", label: STATUS_COPY.failed, progress: job.progress };
  }
  if (job && ACTIVE_JOB_STATUSES.has(job.status)) {
    const key = job.stage in STATUS_COPY ? job.stage : job.status;
    return { key: "working", label: STATUS_COPY[key] || "构建中", progress: job.progress };
  }
  if (version?.technical_status === "searchable" || job?.status === "succeeded") {
    return { key: "ready", label: STATUS_COPY.succeeded, progress: 100 };
  }
  return { key: "working", label: "等待处理", progress: job?.progress || 0 };
}

export function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
