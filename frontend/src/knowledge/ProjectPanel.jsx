import { useState } from "react";
import { FolderOpen, Pencil, Plus } from "lucide-react";
import { createProject, renameProject } from "../api";

export default function ProjectPanel({
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
        {projects.map((project) => {
          if (editingId === project.id) return (
            <form className="inline-name-form project-rename" key={project.id} onSubmit={(event) => submitRename(event, project.id)}>
              <input autoFocus value={editingName} onChange={(event) => setEditingName(event.target.value)} maxLength="200" />
              <button type="submit" disabled={saving || !editingName.trim()}>保存</button>
              <button type="button" onClick={() => setEditingId(null)}>取消</button>
            </form>
          );
          return (
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
          );
        })}
        {!projects.length && !creating && (
          <button className="empty-projects" type="button" onClick={() => setCreating(true)}>
            <Plus size={16} />创建第一个知识库
          </button>
        )}
      </div>
    </aside>
  );
}
