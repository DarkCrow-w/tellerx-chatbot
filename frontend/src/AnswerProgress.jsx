import { useEffect, useState } from "react";
import { Check, ChevronDown, LoaderCircle, Square } from "lucide-react";

/** Execution events only; elapsed time never advances the reported stage. */
export default function AnswerProgress({ events = [], startedAt, duration, active = false, onStop, failed = false }) {
  const [open, setOpen] = useState(active);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  const elapsed = active ? now - startedAt : duration || 0;
  const seconds = Math.max(0, Math.floor(elapsed / 1000));
  let title = "处理过程";
  if (active) title = "正在思考";
  else if (failed) title = "处理已结束";
  const current = events.at(-1)?.text || "正在连接知识库…";
  return (
    <div className={`answer-progress ${active ? "is-active" : ""}`}>
      <div className="progress-heading">
        <button type="button" className="progress-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          {active ? <LoaderCircle size={15} className="progress-spinner" /> : <Check size={15} />}
          <span>{title}</span>
          <ChevronDown size={14} className={open ? "rotated" : ""} />
        </button>
        {active && <button className="progress-stop" type="button" onClick={onStop}><Square size={10} />停止等待</button>}
      </div>
      {active && <div className="progress-current" role="status">{current}</div>}
      {open && <ol className="progress-steps">
        {events.map((event, index) => (
          <li key={index} className={active && index === events.length - 1 ? "current" : ""}>
            <span className="progress-dot" /><div><span>{event.text}</span>
              {event.details?.slice(0, 3).map((detail, detailIndex) => (
                <small className="progress-detail" key={detailIndex}>{detail}</small>
              ))}
            </div>
          </li>
        ))}
      </ol>}
      {active && seconds >= 20 && <p className="progress-note">仍在处理，完成后会自动显示答案。你可以继续查看上述进度。</p>}
    </div>
  );
}
