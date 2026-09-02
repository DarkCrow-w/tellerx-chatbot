import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Download,
  File,
  FilePlus2,
  FolderOpen,
  LoaderCircle,
  Menu,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Square,
  Trash2,
  UploadCloud,
  XCircle,
} from "lucide-react";

import {
  approveDocumentVersion,
  bulkDeleteDocuments,
  cleanupProject,
  createProject,
  deleteDocument,
  deleteProject,
  deprecateDocumentVersion,
  documentDownloadUrl,
  getDocumentCapabilities,
  listDocuments,
  listDocumentVersions,
  renameProject,
  retryIngestionJob,
} from "./api";
import {
  formatBytes,
  friendlyUploadError,
  prepareFiles,
  processUploadQueue,
} from "./knowledge/upload";

const PAGE_SIZE = 20;
const ACTIVE_JOB_STATUSES = new Set(["queued", "running", "index_pending"]);

const STATUS_COPY = {
  queued: "等待处理",
  starting: "准备构建",
  parsing: "解析中",
  embedding: "向量化中",
  indexing: "建立索引",
  succeeded: "可检索",
  failed: "构建失败",
  deprecated: "已废弃",
};

function displayWarning(warning) {
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

function documentState(document) {
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

function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function BatchStatus({ entries, running, onStop }) {
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

function ProjectPanel({
  projects,
  projectId,
  onSelect,
  onCreated,
  onRenamed,
  onToast,
}) {
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [editingId, setEditingId] = useState(null);
  const [editingName, setEditingName] = useState("");
  const [saving, setSaving] = useState(false);

  async function submitNew(event) {
    event.preventDefault();
    if (!newName.trim() || saving) return;
    setSaving(true);
    try {
      const project = await createProject(newName.trim());
      setNewName("");
      setCreating(false);
      await onCreated(project.id);
      onToast("知识库已创建");
    } catch (error) {
      onToast(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function submitRename(event, projectIdToRename) {
    event.preventDefault();
    if (!editingName.trim() || saving) return;
    setSaving(true);
    try {
      await renameProject(projectIdToRename, editingName.trim());
      setEditingId(null);
      await onRenamed(projectIdToRename);
      onToast("知识库名称已更新");
    } catch (error) {
      onToast(error.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <aside className="project-panel">
      <header>
        <span>知识库</span>
        <button className="mini-icon-button" type="button" onClick={() => setCreating(true)} aria-label="新建知识库">
          <Plus size={15} />
        </button>
      </header>
      {creating && (
        <form className="inline-name-form" onSubmit={submitNew}>
          <input autoFocus value={newName} onChange={(event) => setNewName(event.target.value)} maxLength="200" placeholder="知识库名称" />
          <button type="submit" disabled={saving || !newName.trim()}>创建</button>
          <button type="button" onClick={() => setCreating(false)}>取消</button>
        </form>
      )}
      <div className="project-list">
        {projects.map((project) => editingId === project.id ? (
          <form className="inline-name-form project-rename" key={project.id} onSubmit={(event) => submitRename(event, project.id)}>
            <input autoFocus value={editingName} onChange={(event) => setEditingName(event.target.value)} maxLength="200" />
            <button type="submit" disabled={saving || !editingName.trim()}>保存</button>
            <button type="button" onClick={() => setEditingId(null)}>取消</button>
          </form>
        ) : (
          <div className={`project-row ${project.id === projectId ? "active" : ""}`} key={project.id}>
            <button type="button" onClick={() => onSelect(project.id)} title={project.name}>
              <FolderOpen size={15} /><span>{project.name}</span>
            </button>
            <button
              className="mini-icon-button rename-project"
              type="button"
              onClick={() => { setEditingId(project.id); setEditingName(project.name); }}
              aria-label={`重命名 ${project.name}`}
            >
              <Pencil size={13} />
            </button>
          </div>
        ))}
        {!projects.length && !creating && (
          <button className="empty-projects" type="button" onClick={() => setCreating(true)}>
            <Plus size={16} />创建第一个知识库
          </button>
        )}
      </div>
    </aside>
  );
}

function VersionList({ document, versions, onAction, busy }) {
  return (
    <div className="version-list">
      {versions.map((version) => (
        <div className="version-row" key={version.id}>
          <div>
            <strong>{version.version_label || `版本 ${version.id.slice(0, 8)}`}</strong>
            <span>{formatTime(version.indexed_at || version.searchable_at || version.effective_at)}</span>
          </div>
          <div className="version-tags">
            <span>{version.lifecycle_status === "approved" ? "已批准" : version.lifecycle_status === "draft" ? "草稿" : "已废弃"}</span>
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

function DocumentRow({
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

export default function KnowledgeManager({
  projects,
  projectId,
  onProjectChange,
  onProjectsChanged,
  onOpenSidebar,
  onToast,
}) {
  const [capabilities, setCapabilities] = useState(null);
  const [documents, setDocuments] = useState([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [batch, setBatch] = useState([]);
  const [batchRunning, setBatchRunning] = useState(false);
  const [versionLabel, setVersionLabel] = useState("");
  const [owner, setOwner] = useState("");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [expandedId, setExpandedId] = useState(null);
  const [versionsByDocument, setVersionsByDocument] = useState({});
  const [busyId, setBusyId] = useState(null);
  const [selectedDocumentIds, setSelectedDocumentIds] = useState(() => new Set());
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const [projectAction, setProjectAction] = useState(null);
  const [projectConfirmAction, setProjectConfirmAction] = useState(null);
  const [projectConfirmationName, setProjectConfirmationName] = useState("");
  const fileInputRef = useRef(null);
  const folderInputRef = useRef(null);
  const versionInputRef = useRef(null);
  const replacementRef = useRef(null);
  const stopRef = useRef(false);
  const selectedProject = projects.find((project) => project.id === projectId);

  useEffect(() => {
    getDocumentCapabilities().then(setCapabilities).catch((error) => onToast(error.message));
  }, [onToast]);

  useEffect(() => {
    if (!projectId && projects.length) onProjectChange(projects[0].id);
  }, [projectId, projects, onProjectChange]);

  const loadDocuments = useCallback(async (silent = false) => {
    if (!projectId) {
      setDocuments([]);
      setTotal(0);
      return;
    }
    if (!silent) setLoading(true);
    try {
      const page = await listDocuments({ projectId, query, limit: PAGE_SIZE, offset });
      setDocuments(page.items);
      setTotal(page.total);
      const visibleIds = new Set(page.items.map((document) => document.id));
      setSelectedDocumentIds((current) => {
        const retained = [...current].filter((documentId) => visibleIds.has(documentId));
        return retained.length === current.size ? current : new Set(retained);
      });
    } catch (error) {
      onToast(error.message);
    } finally {
      if (!silent) setLoading(false);
    }
  }, [offset, onToast, projectId, query]);

  useEffect(() => {
    const timer = globalThis.setTimeout(() => loadDocuments(), 250);
    return () => globalThis.clearTimeout(timer);
  }, [loadDocuments]);

  useEffect(() => {
    const hasActiveJobs = documents.some((document) => ACTIVE_JOB_STATUSES.has(document.latest_job?.status));
    if (!hasActiveJobs) return undefined;
    const timer = globalThis.setInterval(() => loadDocuments(true), 2500);
    return () => globalThis.clearInterval(timer);
  }, [documents, loadDocuments]);

  useEffect(() => setOffset(0), [projectId, query]);
  useEffect(() => setSelectedDocumentIds(new Set()), [projectId, query, offset]);

  function updateBatchEntry(id, patch) {
    setBatch((items) => items.map((item) => item.id === id ? { ...item, ...patch } : item));
  }

  async function startBatch(files, fixedLogicalKey = null) {
    if (!selectedProject) {
      onToast("请先选择知识库");
      return;
    }
    if (!capabilities) {
      onToast("正在读取上传限制，请稍后再试");
      return;
    }
    if (batchRunning) {
      onToast("请等待当前批次结束");
      return;
    }
    const entries = prepareFiles(files, capabilities, fixedLogicalKey);
    if (!entries.length) return;
    setBatch(entries);
    const accepted = entries.filter((entry) => entry.status === "queued");
    if (!accepted.length) {
      onToast("所选内容中没有可上传的文件");
      return;
    }
    stopRef.current = false;
    setBatchRunning(true);
    try {
      await processUploadQueue({
        entries,
        projectName: selectedProject.name,
        versionLabel,
        owner,
        stopRequested: () => stopRef.current,
        onChange: updateBatchEntry,
      });
      await loadDocuments(true);
      await onProjectsChanged(projectId);
    } finally {
      setBatchRunning(false);
    }
  }

  async function toggleVersions(document) {
    if (expandedId === document.id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(document.id);
    if (versionsByDocument[document.id]) return;
    try {
      const versions = await listDocumentVersions(document.id);
      setVersionsByDocument((items) => ({ ...items, [document.id]: versions }));
    } catch (error) {
      onToast(error.message);
    }
  }

  async function documentAction(action, document, version = null) {
    if (action === "replace") {
      replacementRef.current = document;
      versionInputRef.current?.click();
      return;
    }
    if (action === "delete" && !globalThis.confirm(`确认删除“${document.filename}”吗？文档将从检索中移除。`)) return;
    if (action === "deprecate" && !globalThis.confirm(`确认废弃版本“${version.version_label || version.id.slice(0, 8)}”吗？`)) return;
    setBusyId(document.id);
    try {
      if (action === "retry") await retryIngestionJob(document.latest_job.id);
      if (action === "delete") await deleteDocument(document.id);
      if (action === "approve") await approveDocumentVersion(version.id);
      if (action === "deprecate") await deprecateDocumentVersion(version.id);
      setVersionsByDocument((items) => {
        const next = { ...items };
        delete next[document.id];
        return next;
      });
      await loadDocuments(true);
      onToast(action === "delete" ? "文档已删除" : action === "retry" ? "已重新开始构建" : "版本状态已更新");
    } catch (error) {
      onToast(error.message);
    } finally {
      setBusyId(null);
    }
  }

  function toggleDocumentSelection(documentId) {
    setSelectedDocumentIds((current) => {
      const next = new Set(current);
      if (next.has(documentId)) next.delete(documentId);
      else next.add(documentId);
      return next;
    });
  }

  function toggleCurrentPage() {
    const allSelected = documents.length > 0
      && documents.every((document) => selectedDocumentIds.has(document.id));
    setSelectedDocumentIds(
      allSelected ? new Set() : new Set(documents.map((document) => document.id)),
    );
  }

  async function deleteSelectedDocuments() {
    if (!selectedProject || !selectedDocumentIds.size || bulkDeleting) return;
    const selected = documents.filter((document) => selectedDocumentIds.has(document.id));
    const preview = selected.slice(0, 4).map((document) => `“${document.filename}”`).join("、");
    const remainder = selected.length > 4 ? `等 ${selected.length} 份文档` : `${selected.length} 份文档`;
    if (!globalThis.confirm(
      `确认批量删除${preview ? `${preview} ${remainder}` : remainder}吗？这些文档将从检索中移除。`,
    )) return;

    setBulkDeleting(true);
    try {
      const result = await bulkDeleteDocuments(projectId, [...selectedDocumentIds]);
      setSelectedDocumentIds(new Set());
      setVersionsByDocument((items) => {
        const next = { ...items };
        result.deleted_ids.forEach((documentId) => delete next[documentId]);
        return next;
      });
      const nextTotal = Math.max(0, total - result.deleted_count);
      const nextLastPage = Math.max(
        0,
        Math.floor(Math.max(nextTotal - 1, 0) / PAGE_SIZE) * PAGE_SIZE,
      );
      if (offset > nextLastPage) setOffset(nextLastPage);
      else await loadDocuments(true);
      await onProjectsChanged(projectId);
      onToast(
        result.skipped_count
          ? `已删除 ${result.deleted_count} 份，跳过 ${result.skipped_count} 份`
          : `已删除 ${result.deleted_count} 份文档`,
      );
    } catch (error) {
      onToast(error.message);
    } finally {
      setBulkDeleting(false);
    }
  }

  function openProjectConfirmation(action) {
    if (!selectedProject || projectAction || batchRunning) return;
    setProjectConfirmationName("");
    setProjectConfirmAction(action);
  }

  async function runProjectAction() {
    if (!selectedProject || !projectConfirmAction || projectAction || batchRunning) return;
    const action = projectConfirmAction;
    const deleting = action === "delete";
    if (projectConfirmationName.trim() !== selectedProject.name) return;

    setProjectAction(action);
    try {
      const result = deleting
        ? await deleteProject(selectedProject.id)
        : await cleanupProject(selectedProject.id);
      if (deleting) {
        setDocuments([]);
        setTotal(0);
      } else {
        await loadDocuments(true);
      }
      setBatch([]);
      setExpandedId(null);
      setVersionsByDocument({});
      setSelectedDocumentIds(new Set());
      setOffset(0);
      setProjectConfirmAction(null);
      setProjectConfirmationName("");
      await onProjectsChanged(deleting ? null : selectedProject.id);
      const fileWarning = result.files_failed
        ? `，另有 ${result.files_failed} 个文件清理失败，请查看后端日志`
        : "";
      onToast(
        deleting
          ? `知识库已彻底删除，共清理 ${result.documents_deleted} 份文档${fileWarning}`
          : `已清理 ${result.documents_deleted} 份删除残留${fileWarning}`,
      );
    } catch (error) {
      onToast(error.message);
    } finally {
      setProjectAction(null);
    }
  }

  function selectFiles(event, fixedLogicalKey = null) {
    const files = event.target.files;
    if (files?.length) startBatch(files, fixedLogicalKey);
    event.target.value = "";
  }

  const accept = capabilities?.allowed_extensions.join(",") || undefined;
  const lastPage = Math.max(0, Math.floor(Math.max(total - 1, 0) / PAGE_SIZE) * PAGE_SIZE);
  const currentPageSelected = documents.length > 0
    && documents.every((document) => selectedDocumentIds.has(document.id));

  return (
    <main className="workspace knowledge-workspace">
      <header className="topbar manager-topbar">
        <button className="icon-button menu-button" type="button" onClick={onOpenSidebar} aria-label="打开侧栏"><Menu size={19} /></button>
        <div><strong>知识库管理</strong><span>上传、构建并维护企业文档</span></div>
      </header>

      <section className="manager-scroll">
        <div className="manager-page">
          <ProjectPanel
            projects={projects}
            projectId={projectId}
            onSelect={onProjectChange}
            onCreated={onProjectsChanged}
            onRenamed={onProjectsChanged}
            onToast={onToast}
          />

          <section className="document-panel">
            <header className="document-panel-header">
              <div>
                <h1>{selectedProject?.name || "选择知识库"}</h1>
                <p>{selectedProject ? `${total} 份文档` : "创建或选择一个知识库后开始上传"}</p>
              </div>
              <div className="upload-buttons">
                <button className="secondary-button danger-button" type="button" disabled={!selectedProject || batchRunning || !!projectAction} onClick={() => openProjectConfirmation("cleanup")}>
                  {projectAction === "cleanup" ? <LoaderCircle className="spin" size={15} /> : <Archive size={15} />}清理删除残留
                </button>
                <button className="secondary-button danger-button" type="button" disabled={!selectedProject || batchRunning || !!projectAction} onClick={() => openProjectConfirmation("delete")}>
                  {projectAction === "delete" ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />}删除知识库
                </button>
                <button className="secondary-button" type="button" disabled={!selectedProject || batchRunning || !!projectAction} onClick={() => fileInputRef.current?.click()}>
                  <FilePlus2 size={15} />选择文件
                </button>
                <button className="primary-button" type="button" disabled={!selectedProject || batchRunning || !!projectAction} onClick={() => folderInputRef.current?.click()}>
                  <FolderOpen size={15} />选择文件夹
                </button>
              </div>
            </header>

            <input ref={fileInputRef} className="visually-hidden" aria-hidden="true" tabIndex="-1" type="file" multiple accept={accept} onChange={selectFiles} />
            <input ref={folderInputRef} className="visually-hidden" aria-hidden="true" tabIndex="-1" type="file" multiple accept={accept} webkitdirectory="" directory="" onChange={selectFiles} />
            <input ref={versionInputRef} className="visually-hidden" aria-hidden="true" tabIndex="-1" type="file" accept={accept} onChange={(event) => selectFiles(event, replacementRef.current?.logical_key)} />

            {selectedProject && (
              <div
                className={`upload-zone ${dragging ? "dragging" : ""}`}
                onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
                onDragOver={(event) => event.preventDefault()}
                onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false); }}
                onDrop={(event) => {
                  event.preventDefault();
                  setDragging(false);
                  if (event.dataTransfer.files.length) startBatch(event.dataTransfer.files);
                }}
              >
                <UploadCloud size={22} />
                <div><strong>拖放多个文档到这里</strong><span>或使用上方按钮选择文件和完整文件夹</span></div>
                {capabilities && <small>支持 {capabilities.allowed_extensions.join("、")} · 单文件不超过 {formatBytes(capabilities.max_upload_bytes)}</small>}
              </div>
            )}

            {selectedProject && (
              <div className="advanced-upload">
                <button type="button" onClick={() => setAdvancedOpen((value) => !value)}>
                  <ChevronDown size={14} />上传高级设置
                </button>
                {advancedOpen && (
                  <div>
                    <label><span>版本标签（可选）</span><input value={versionLabel} onChange={(event) => setVersionLabel(event.target.value)} maxLength="100" placeholder="例如 2026.09" /></label>
                    <label><span>文档所有者（可选）</span><input value={owner} onChange={(event) => setOwner(event.target.value)} maxLength="200" placeholder="例如 财务部" /></label>
                  </div>
                )}
              </div>
            )}

            <BatchStatus entries={batch} running={batchRunning} onStop={() => { stopRef.current = true; }} />

            {selectedProject && (
              <div className="document-toolbar">
                <label className="document-search"><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索文件名或目录路径" /></label>
                <button className="mini-icon-button" type="button" onClick={() => loadDocuments()} aria-label="刷新文档列表"><RefreshCw size={14} /></button>
                {!!documents.length && (
                  <div className="document-selection">
                    <label>
                      <input
                        ref={(input) => {
                          if (input) {
                            input.indeterminate = selectedDocumentIds.size > 0
                              && !currentPageSelected;
                          }
                        }}
                        type="checkbox"
                        checked={currentPageSelected}
                        disabled={bulkDeleting}
                        onChange={toggleCurrentPage}
                      />
                      本页全选
                    </label>
                    {selectedDocumentIds.size > 0 && (
                      <button className="bulk-delete-button" type="button" disabled={bulkDeleting} onClick={deleteSelectedDocuments}>
                        {bulkDeleting ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}
                        删除所选（{selectedDocumentIds.size}）
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}

            <div className={`document-list ${loading ? "loading" : ""}`}>
              {documents.map((document) => (
                <DocumentRow
                  key={document.id}
                  document={document}
                  expanded={expandedId === document.id}
                  versions={versionsByDocument[document.id]}
                  busy={bulkDeleting || !!projectAction || busyId === document.id}
                  selected={selectedDocumentIds.has(document.id)}
                  onSelect={() => toggleDocumentSelection(document.id)}
                  onToggle={() => toggleVersions(document)}
                  onAction={(action, version) => documentAction(action, document, version)}
                />
              ))}
              {selectedProject && !loading && !documents.length && (
                <div className="empty-documents"><FolderOpen size={28} /><strong>这个知识库还没有文档</strong><span>上传文件或文件夹后，系统会自动解析并建立索引。</span></div>
              )}
              {!selectedProject && (
                <div className="empty-documents"><FolderOpen size={28} /><strong>尚未选择知识库</strong><span>从左侧选择已有知识库，或者创建一个新的知识库。</span></div>
              )}
            </div>

            {total > PAGE_SIZE && (
              <nav className="pagination" aria-label="文档分页">
                <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft size={14} />上一页</button>
                <span>第 {Math.floor(offset / PAGE_SIZE) + 1} / {Math.floor(lastPage / PAGE_SIZE) + 1} 页</span>
                <button type="button" disabled={offset >= lastPage} onClick={() => setOffset(Math.min(lastPage, offset + PAGE_SIZE))}>下一页<ChevronRight size={14} /></button>
              </nav>
            )}
          </section>
        </div>
      </section>

      {projectConfirmAction && selectedProject && (
        <div className="dialog-backdrop" role="presentation">
          <section className="danger-dialog" role="dialog" aria-modal="true" aria-labelledby="project-danger-title">
            <div className="danger-dialog-icon"><AlertTriangle size={20} /></div>
            <div>
              <h2 id="project-danger-title">
                {projectConfirmAction === "delete" ? "彻底删除知识库" : "清理删除残留"}
              </h2>
              <p>
                {projectConfirmAction === "delete"
                  ? `将永久删除“${selectedProject.name}”中的全部原文件、版本、分块和无引用向量，知识库本身也会被删除。`
                  : `将永久回收“${selectedProject.name}”中已经删除的文档残留；仍在使用的文档不会受到影响。`}
                操作无法恢复。
              </p>
              <label>
                <span>请输入知识库名称以确认</span>
                <input
                  autoFocus
                  value={projectConfirmationName}
                  onChange={(event) => setProjectConfirmationName(event.target.value)}
                  placeholder={selectedProject.name}
                  aria-label="输入知识库名称确认"
                  disabled={!!projectAction}
                />
              </label>
              <div className="danger-dialog-actions">
                <button
                  className="secondary-button"
                  type="button"
                  disabled={!!projectAction}
                  onClick={() => setProjectConfirmAction(null)}
                >
                  取消
                </button>
                <button
                  className="danger-confirm-button"
                  type="button"
                  disabled={!!projectAction || projectConfirmationName.trim() !== selectedProject.name}
                  onClick={runProjectAction}
                >
                  {projectAction && <LoaderCircle className="spin" size={14} />}
                  {projectConfirmAction === "delete" ? "确认彻底删除" : "确认清理"}
                </button>
              </div>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
