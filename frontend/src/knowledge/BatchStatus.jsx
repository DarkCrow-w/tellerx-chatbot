import { useMemo } from "react";
import { Archive, CheckCircle2, File, LoaderCircle, Square, XCircle } from "lucide-react";
import { formatBytes } from "./upload";
import { STATUS_COPY } from "./documentDisplay";

export default function BatchStatus({ entries, running, onStop }) {
  const counts = useMemo(() => entries.reduce((result, entry) => {
    result[entry.status] = (result[entry.status] || 0) + 1;
    return result;
  }, {}), [entries]);
  if (!entries.length) return null;

  return (
    <section className="batch-card" aria-live="polite">
      <header>
        <div>
          <strong>{running ? "正在构建知识库" : "本次上传结果"}</strong>
          <span>
            成功 {counts.succeeded || 0} · 已存在 {counts.duplicate || 0} ·
            跳过 {(counts.skipped || 0) + (counts.cancelled || 0)} · 失败 {counts.failed || 0}
          </span>
        </div>
        {running && (
          <button className="secondary-button" type="button" onClick={onStop}>
            <Square size={13} />停止后续上传
          </button>
        )}
      </header>
      <div className="batch-list">
        {entries.map((entry) => (
          <div className={`batch-item ${entry.status}`} key={entry.id}>
            <span className="batch-file-icon"><File size={15} /></span>
            <span className="batch-file-copy">
              <strong title={entry.logicalKey}>{entry.logicalKey}</strong>
              <small>{entry.reason || STATUS_COPY[entry.job?.stage] || entry.job?.stage || formatBytes(entry.file.size)}</small>
            </span>
            <span className="batch-progress">
              {entry.status === "succeeded" && <CheckCircle2 size={16} />}
              {entry.status === "duplicate" && <Archive size={16} />}
              {["failed", "skipped", "cancelled"].includes(entry.status) && <XCircle size={16} />}
              {["queued", "uploading", "processing"].includes(entry.status) && (
                <><LoaderCircle className="spin" size={15} /><em>{entry.progress}%</em></>
              )}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}
