import { useId, useRef, useState } from "react";
import { Download } from "lucide-react";
import { getSectionContext, downloadEvidenceDocument } from "./api";

function ContextChunk({ chunk, sourceSectionId }) {
  const title = chunk.breadcrumb?.length
    ? chunk.breadcrumb.join(" › ")
    : chunk.heading_path || "正文（无章节标题）";
  const isAdjacent = chunk.section_id && chunk.section_id !== sourceSectionId;
  return (
    <div className="context-chunk">
      <h5>{title}{isAdjacent ? " · 相邻章节" : ""}</h5>
      <p>{chunk.content}</p>
    </div>
  );
}

export default function SourceCard({ source, onToast }) {
  const [open, setOpen] = useState(false);
  const [context, setContext] = useState(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const pending = useRef(false);
  const contextId = useId();
  const location = [
    source.breadcrumb?.length ? source.breadcrumb.join(" › ") : source.heading_path,
    source.page_number && `第 ${source.page_number} 页`, source.sheet_name, source.cell_range,
  ].filter(Boolean).join(" · ");

  const contextTitle = context?.section?.heading_path || context?.section?.title
    || source.heading_path || "正文（无章节标题）";

  async function toggleContext() {
    setOpen(!open);
    if (open || context || pending.current) return;
    pending.current = true;
    setLoading(true);
    try {
      setContext(await getSectionContext(source.section_id));
    } catch (error) {
      setOpen(false);
      onToast?.(error.message);
    } finally {
      pending.current = false;
      setLoading(false);
    }
  }

  async function download() {
    setDownloading(true);
    try {
      await downloadEvidenceDocument(source.chunk_id);
    } catch (error) {
      onToast?.(`下载失败：${error.message}`);
    } finally {
      setDownloading(false);
    }
  }

  return (
    <article className="source-card">
      <header><strong title={source.filename}>{source.filename}</strong>{location && <span>{location}</span>}</header>
      <p>{source.quote}</p>
      <div className="source-actions">
        {source.section_id && <button className="section-context-trigger" type="button"
          aria-expanded={open} aria-controls={contextId} onClick={toggleContext}>
          {open ? "收起章节上下文" : "查看章节上下文"}
        </button>}
        {source.chunk_id && <button className="source-download" type="button" onClick={download} disabled={downloading}
          title={`下载证据对应的文档版本：${source.filename}`}>
          <Download size={13} />{downloading ? "正在下载…" : "下载文档"}
        </button>}
      </div>
      {open && <div className="section-context" id={contextId} aria-busy={loading}>
        {loading && <p role="status">正在加载章节上下文…</p>}
        {context && <>
          <h4>{contextTitle}</h4>
          {context.chunks.map((chunk) => (
            <ContextChunk key={chunk.chunk_id} chunk={chunk} sourceSectionId={source.section_id} />
          ))}
          {!context.chunks.length && <p>该章节暂无可展示内容。</p>}
          {context.truncated && <small>章节过长，仅展示前 100 个内容块及相邻上下文。</small>}
        </>}
      </div>}
    </article>
  );
}
