import {
  AlertTriangle,
  ChevronDown,
  Download,
  File,
  FilePlus2,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { documentDownloadUrl } from "../api";
import { friendlyUploadError } from "./upload";
import { displayWarning, documentState, formatTime } from "./documentDisplay";
import VersionList from "./VersionList";

export default function DocumentRow({
  document,
  expanded,
  versions,
  busy,
  selected,
  onSelect,
  onToggle,
  onAction,
}) {
  const state = documentState(document);
  const warnings = document.latest_job?.warnings || document.latest_version?.parse_warnings || [];
  return (
    <article className={`document-row ${expanded ? "expanded" : ""} ${selected ? "selected" : ""}`}>
      <div className="document-main">
        <input
          className="document-checkbox"
          type="checkbox"
          checked={selected}
          disabled={busy}
          onChange={onSelect}
          aria-label={`选择 ${document.filename}`}
        />
        <button className="document-expand" type="button" onClick={onToggle} aria-label="查看版本">
          <ChevronDown size={15} />
        </button>
        <span className="document-icon"><File size={18} /></span>
        <div className="document-copy">
          <strong title={document.logical_key}>{document.filename}</strong>
          <span>{document.logical_key} · {document.version_count} 个版本 · {formatTime(document.updated_at)}</span>
          {state.key === "working" && (
            <div className="inline-progress"><i style={{ width: `${state.progress}%` }} /></div>
          )}
          {document.latest_job?.error_message && <small className="document-error">{friendlyUploadError(document.latest_job.error_message)}</small>}
          {!!warnings.length && (
            <small className="document-warning"><AlertTriangle size={12} />{warnings.map(displayWarning).join("；")}</small>
          )}
        </div>
        <span className={`document-status ${state.key}`}><i />{state.label}</span>
        <div className="document-actions">
          {document.latest_job?.status === "failed" && (
            <button className="text-button" type="button" disabled={busy} onClick={() => onAction("retry")}>
              <RefreshCw size={13} />重试
            </button>
          )}
          <button className="text-button" type="button" disabled={busy} onClick={() => onAction("replace")}>
            <FilePlus2 size={13} />新版本
          </button>
          <a className="text-button" href={documentDownloadUrl(document.id)}><Download size={13} />下载</a>
          <button className="text-button danger-text" type="button" disabled={busy} onClick={() => onAction("delete")}>
            <Trash2 size={13} />删除
          </button>
        </div>
      </div>
      {expanded && <VersionList document={document} versions={versions || []} onAction={onAction} busy={busy} />}
    </article>
  );
}
