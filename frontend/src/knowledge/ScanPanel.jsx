import { useEffect, useRef, useState } from "react";
import { LoaderCircle, Search } from "lucide-react";
import { getKnowledgeScan, startKnowledgeScan } from "../api";

export default function ScanPanel({ projectId, onUpdated, onToast }) {
  const [state, setState] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [starting, setStarting] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const callbacks = useRef({ onUpdated, onToast });
  callbacks.current = { onUpdated, onToast };
  useEffect(() => {
    let disposed = false;
    let timer;
    setState(null);
    setLoadError(null);
    async function poll() {
      try {
        const result = await getKnowledgeScan(projectId);
        if (disposed) return;
        setState(result);
        callbacks.current.onUpdated();
        if (result.scan?.status === "running") timer = setTimeout(poll, 2500);
      } catch (error) {
        if (!disposed) { setLoadError(error.message); callbacks.current.onToast(error.message); }
      }
    }
    poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [projectId, refresh]);

  async function start() {
    setStarting(true);
    try {
      await startKnowledgeScan(projectId);
      setRefresh((value) => value + 1);
    } catch (error) {
      callbacks.current.onToast(error.message);
    } finally {
      setStarting(false);
    }
  }
  const scan = state?.scan;
  const running = starting || scan?.status === "running";
  return (
    <section className="scan-panel" aria-label="扫描 MinIO 知识库">
      <div className="scan-heading">
        <div>
          <strong>从 MinIO 导入文档</strong>
          <p>将文档放入下方目录，然后扫描。支持子目录，已解析且内容未变化的文档会跳过。</p>
        </div>
        <button className="secondary-button" type="button" onClick={start} disabled={!state?.enabled || running}>
          {running ? <LoaderCircle className="spin" size={15} /> : <Search size={15} />}
          {running ? "扫描处理中…" : "扫描知识库"}
        </button>
      </div>
      {loadError && <p role="alert">{loadError} <button type="button" className="secondary-button" onClick={() => setRefresh((value) => value + 1)}>重新加载</button></p>}
      {state && <code className="scan-path">{state.bucket}/{state.prefix}</code>}
      {state && !state.enabled && <p>请先启用 MinIO/S3 存储和本地后台入库模式。</p>}
      {scan && <div aria-live="polite">
        <p>{scan.status === "running" ? "扫描中" : scan.status === "failed" ? "扫描中断" : "扫描完成"}：已检查 {scan.processed} 个，已解析 {scan.imported} 个，跳过 {scan.skipped} 个，失败 {scan.failed} 个。</p>
        {scan.current_key && <p className="scan-path">正在处理：{scan.current_key}</p>}
        {scan.error && <p role="alert">{scan.error}</p>}
        {scan.details?.length > 0 && <details><summary>查看跳过或失败明细（最近 100 条）</summary>
          <ul>{scan.details.map((item, index) => <li key={`${item.key}-${index}`}>{item.key}：{item.detail}</li>)}</ul>
        </details>}
      </div>}
    </section>
  );
}
