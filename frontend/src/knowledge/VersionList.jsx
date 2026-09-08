import { Archive, CheckCircle2, Download } from "lucide-react";
import { documentDownloadUrl } from "../api";
import { formatTime } from "./documentDisplay";

const LIFECYCLE_LABELS = { approved: "已批准", draft: "草稿" };

export default function VersionList({ document, versions, onAction, busy }) {
  return (
    <div className="version-list">
      {versions.map((version) => (
        <div className="version-row" key={version.id}>
          <div>
            <strong>{version.version_label || `版本 ${version.id.slice(0, 8)}`}</strong>
            <span>{formatTime(version.indexed_at || version.searchable_at || version.effective_at)}</span>
          </div>
          <div className="version-tags">
            <span>{LIFECYCLE_LABELS[version.lifecycle_status] || "已废弃"}</span>
            <span>{version.technical_status === "searchable" ? "可检索" : version.technical_status}</span>
            {version.is_current && <span className="current-tag">当前生效</span>}
          </div>
          <div className="version-actions">
            <a className="text-button" href={documentDownloadUrl(document.id, version.id)}><Download size={13} />下载</a>
            {version.lifecycle_status === "draft" && version.technical_status === "searchable" && (
              <button className="text-button" type="button" disabled={busy} onClick={() => onAction("approve", version)}>
                <CheckCircle2 size={13} />批准
              </button>
            )}
            {version.lifecycle_status !== "deprecated" && (
              <button className="text-button danger-text" type="button" disabled={busy} onClick={() => onAction("deprecate", version)}>
                <Archive size={13} />废弃
              </button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
