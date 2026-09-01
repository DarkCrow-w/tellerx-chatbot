import { getIngestionJob, uploadDocument } from "../api.js";

const HIDDEN_NAMES = new Set([".ds_store", "thumbs.db", "desktop.ini"]);

/** 根据后缀生成稳定、可读的文档类别，不把技术细节暴露给上传用户。 */
export function inferDocumentType(filename) {
  const suffix = extensionOf(filename);
  if ([".xlsx", ".xlsm", ".csv"].includes(suffix)) return "spreadsheet";
  if ([".md", ".markdown", ".txt"].includes(suffix)) return "text-document";
  if ([".html", ".htm"].includes(suffix)) return "web-document";
  if (suffix === ".pdf") return "pdf-document";
  if (suffix === ".docx") return "word-document";
  return "business-document";
}

function extensionOf(filename) {
  const index = filename.lastIndexOf(".");
  return index < 0 ? "" : filename.slice(index).toLowerCase();
}

/**
 * 文件夹上传去掉用户本地最外层目录，内部相对路径作为逻辑身份。
 * 这样同名文件可以共存，本地根目录改名也不会产生一套新文档。
 */
export function logicalKeyFor(file) {
  const relative = (file.webkitRelativePath || "").replaceAll("\\", "/");
  if (!relative) return file.name;
  const segments = relative.split("/").filter(Boolean);
  return segments.length > 1 ? segments.slice(1).join("/") : file.name;
}

/** 在传输前过滤隐藏文件、未知格式和超限文件，并保留每项原因供批次汇总。 */
export function prepareFiles(files, capabilities, fixedLogicalKey = null) {
  const allowed = new Set(capabilities.allowed_extensions.map((item) => item.toLowerCase()));
  return Array.from(files).map((file, index) => {
    const logicalKey = fixedLogicalKey || logicalKeyFor(file);
    const parts = logicalKey.split("/");
    let reason = null;
    if (parts.some((part) => part.startsWith(".")) || HIDDEN_NAMES.has(file.name.toLowerCase())) {
      reason = "已跳过隐藏或系统文件";
    } else if (!allowed.has(extensionOf(file.name))) {
      reason = "不支持此文件格式";
    } else if (file.size > capabilities.max_upload_bytes) {
      reason = `超过单文件上限 ${formatBytes(capabilities.max_upload_bytes)}`;
    }
    return {
      id: `${file.name}-${file.size}-${file.lastModified}-${index}`,
      file,
      logicalKey,
      status: reason ? "skipped" : "queued",
      progress: 0,
      reason,
      job: null,
      duplicate: false,
    };
  });
}

export function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(bytes >= 10 * 1024 ** 2 ? 0 : 1)} MB`;
}

/** 把后端和模型 SDK 的常见英文诊断转换为上传用户能直接处理的提示。 */
export function friendlyUploadError(message) {
  const raw = String(message || "");
  const normalized = raw.toLowerCase();
  if (normalized.includes("no extractable text") || normalized.includes("produced no chunks")) {
    return "文档中没有可提取的文字；扫描版 PDF 需要先完成 OCR";
  }
  if (normalized.includes("unsupported file type")) return "不支持此文件格式";
  if (normalized.includes("exceeds maximum upload size")) return "文件超过服务端允许的大小上限";
  if (normalized.includes("embedding") || normalized.includes("modelapi")) {
    return "Embedding 模型调用失败，请检查内部模型接口和模型配置";
  }
  return raw || "知识库构建失败，请查看后端日志";
}

function pause(milliseconds) {
  return new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
}

async function waitUntilFinished(jobId, onJob) {
  // 构建耗时取决于文档大小和内部模型端点，因此使用任务终态而不是固定超时。
  for (;;) {
    const job = await getIngestionJob(jobId);
    onJob(job);
    if (["succeeded", "failed"].includes(job.status)) return job;
    await pause(1200);
  }
}

/**
 * 使用两个协作 Worker 消费浏览器队列；单项失败被收敛到该项，不会阻断批次。
 * stopRequested 只停止尚未开始的文件，服务端已经接受的不可变任务继续完成。
 */
export async function processUploadQueue({
  entries,
  projectName,
  versionLabel,
  owner,
  stopRequested,
  onChange,
}) {
  const pending = entries.filter((entry) => entry.status === "queued");
  let cursor = 0;

  async function worker() {
    while (cursor < pending.length && !stopRequested()) {
      const entry = pending[cursor];
      cursor += 1;
      onChange(entry.id, { status: "uploading", progress: 2, reason: null });
      try {
        const accepted = await uploadDocument({
          file: entry.file,
          project: projectName,
          documentType: inferDocumentType(entry.file.name),
          logicalKey: entry.logicalKey,
          versionLabel,
          owner,
        });
        onChange(entry.id, {
          status: "processing",
          progress: 5,
          duplicate: accepted.duplicate,
        });
        const job = await waitUntilFinished(accepted.job_id, (nextJob) => {
          onChange(entry.id, {
            status: "processing",
            progress: nextJob.progress,
            job: nextJob,
          });
        });
        if (job.status === "failed") {
          onChange(entry.id, {
            status: "failed",
            progress: job.progress,
            reason: friendlyUploadError(job.error_message),
            job,
          });
        } else {
          onChange(entry.id, {
            status: accepted.duplicate ? "duplicate" : "succeeded",
            progress: 100,
            job,
          });
        }
      } catch (error) {
        onChange(entry.id, {
          status: "failed",
          reason: friendlyUploadError(error.message || "上传失败"),
        });
      }
    }
  }

  await Promise.all([worker(), worker()]);
  if (stopRequested()) {
    for (const entry of pending.slice(cursor)) {
      onChange(entry.id, { status: "cancelled", reason: "已停止，未上传" });
    }
  }
}
