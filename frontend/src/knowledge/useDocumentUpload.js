import { useRef, useState } from "react";
import { prepareFiles, processUploadQueue } from "./upload";

// One batch owns its cancellation flag until the currently running file finishes.
export function useDocumentUpload({
  selectedProject, capabilities, versionLabel, owner, projectId,
  loadDocuments, onProjectsChanged, onToast,
}) {
  const [batch, setBatch] = useState([]);
  const [batchRunning, setBatchRunning] = useState(false);
  const stopRef = useRef(false);

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

  function stopBatch() {
    stopRef.current = true;
  }

  return { batch, setBatch, batchRunning, startBatch, stopBatch };
}
