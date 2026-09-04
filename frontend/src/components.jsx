import { useEffect, useRef, useState } from "react";
import {
  Archive,
  ArrowUp,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clipboard,
  Code2,
  Database,
  FileSearch,
  MessageSquareText,
  Moon,
  PanelLeftClose,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Sun,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";

import { getSectionContext } from "./api";

const STARTERS = [
  {
    icon: FileSearch,
    title: "梳理业务流程",
    description: "总结关键步骤与审批节点",
    prompt: "概括当前项目的核心业务流程，并标出关键审批节点。",
  },
  {
    icon: Code2,
    title: "核对接口规则",
    description: "定位约束与异常处理要求",
    prompt: "根据知识库，列出当前系统最重要的接口规则和异常处理要求。",
  },
  {
    icon: CircleAlert,
    title: "检查文档冲突",
    description: "发现不一致或信息缺口",
    prompt: "知识库中有哪些互相冲突或信息不足的内容？请附原文证据。",
  },
];

const STATUS = {
  answered: { label: "有据可查", className: "grounded" },
  grounded: { label: "有据可查", className: "grounded" },
  conflict: { label: "存在冲突", className: "conflict" },
  insufficient_evidence: { label: "证据不足", className: "insufficient" },
  clarification_required: { label: "请选择文档", className: "conflict" },
};

function SourceList({ sources, onToast }) {
  /** 按需展开引用原文，避免长证据列表压过回答主体。 */
  const [open, setOpen] = useState(false);
  const [contexts, setContexts] = useState({});
  const [loadingSection, setLoadingSection] = useState(null);
  if (!sources?.length) return null;

  async function loadContext(sectionId) {
    if (contexts[sectionId]) return;
    setLoadingSection(sectionId);
    try {
      const context = await getSectionContext(sectionId);
      setContexts((current) => ({ ...current, [sectionId]: context }));
    } catch (error) {
      onToast?.(error.message);
    } finally {
      setLoadingSection(null);
    }
  }

  return (
    <div className={`sources ${open ? "is-open" : ""}`}>
      <button
        className="sources-trigger"
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <span><Archive size={14} /> 原文证据 · {sources.length} 条</span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="source-list">
          {sources.map((source, index) => {
            const location = [
              source.breadcrumb?.length ? source.breadcrumb.join(" › ") : source.heading_path,
              source.page_number && `第 ${source.page_number} 页`,
              source.sheet_name,
              source.cell_range,
            ].filter(Boolean).join(" · ");
            return (
              <article className="source-card" key={`${source.chunk_id || source.filename}-${index}`}>
                <header>
                  <strong>{source.filename}</strong>
                  {location && <span>{location}</span>}
                </header>
                <p>{source.quote}</p>
                {source.section_id && (
                  <button
                    className="section-context-trigger"
                    type="button"
                    onClick={() => loadContext(source.section_id)}
                  >
                    {loadingSection === source.section_id ? "正在加载…" : "查看章节上下文"}
                  </button>
                )}
                {contexts[source.section_id] && (
                  <div className="section-context">
                    {contexts[source.section_id].chunks.map((chunk) => (
                      <p key={chunk.chunk_id}>{chunk.content}</p>
                    ))}
                    {contexts[source.section_id].truncated && <small>章节过长，仅展示前 100 个内容块。</small>}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function AnswerActions({ answer, onToast }) {
  /** 提供回答复制和本地即时反馈状态。 */
  const [vote, setVote] = useState(null);

  async function copyAnswer() {
    /** 使用浏览器剪贴板 API，并为权限失败提供可理解提示。 */
    try {
      await navigator.clipboard.writeText(answer);
      onToast("回答已复制");
    } catch {
      onToast("复制失败，请手动选择文本");
    }
  }

  function rate(value) {
    setVote(value);
    onToast(value === "up" ? "感谢你的反馈" : "已记录，我们会继续改进");
  }

  return (
    <div className="answer-actions">
      <button type="button" onClick={copyAnswer} aria-label="复制回答"><Clipboard size={14} /></button>
      <button className={vote === "up" ? "selected" : ""} type="button" onClick={() => rate("up")} aria-label="回答有帮助"><ThumbsUp size={14} /></button>
      <button className={vote === "down" ? "selected" : ""} type="button" onClick={() => rate("down")} aria-label="回答没帮助"><ThumbsDown size={14} /></button>
    </div>
  );
}

export function Message({ message, onToast, onSelectDocument }) {
  /** 根据消息角色和证据状态渲染用户气泡或助手回答。 */
  if (message.role === "user") {
    return (
      <article className="message user-message">
        <div className="user-bubble">{message.content}</div>
      </article>
    );
  }

  const status = STATUS[message.status] || STATUS.answered;
  return (
    <article className="message assistant-message">
      <div className="assistant-avatar"><span>T</span></div>
      <div className="assistant-content">
        <div className="answer-copy">{message.content}</div>
        {(message.resolvedDocument || message.resolvedScope === "global") && (
          <div className="retrieval-scope">
            检索范围：{message.resolvedScope === "global" ? "全部知识库" : message.resolvedScope}
          </div>
        )}
        {message.clarificationOptions?.length > 0 && (
          <div className="document-options" aria-label="请选择文档">
            {message.clarificationOptions.map((option) => (
              <button
                type="button"
                key={option.document_id}
                onClick={() => onSelectDocument?.(message.originalQuestion, option)}
              >
                <strong>{option.filename}</strong>
                <span>{option.document_type || "文档"}{option.version_label ? ` · ${option.version_label}` : ""}</span>
              </button>
            ))}
          </div>
        )}
        <SourceList sources={message.sources} onToast={onToast} />
        <div className="answer-meta">
          <span className={`answer-status ${status.className}`}><i />{status.label}</span>
          <span>{message.modelId ? `${message.modelId}${message.routeTier ? ` · ${message.routeTier}` : ""}` : "未调用生成模型"}</span>
        </div>
        <AnswerActions answer={message.content} onToast={onToast} />
      </div>
    </article>
  );
}

export function Sidebar({ open, onClose, chats, activeId, onNew, onOpenChat, activeView, onManage, theme, onToggleTheme }) {
  /** 渲染最近对话导航、知识库状态和主题切换。 */
  return (
    <>
      <aside className={`sidebar ${open ? "open" : ""}`} aria-label="对话导航">
        <div className="sidebar-header">
          <a className="brand" href="#/chat" aria-label="TellerX 首页">
            <span className="brand-mark">T</span>
            <span>TellerX</span>
          </a>
          <button className="icon-button close-sidebar" type="button" onClick={onClose} aria-label="关闭侧栏"><PanelLeftClose size={18} /></button>
        </div>

        <button className="new-chat" type="button" onClick={onNew}>
          <Plus size={16} />
          <span>新对话</span>
          <kbd>⌘ K</kbd>
        </button>

        <button className={`manage-nav ${activeView === "knowledge" ? "active" : ""}`} type="button" onClick={onManage}>
          <Database size={16} />
          <span>知识库管理</span>
        </button>

        <nav className="history" aria-label="最近对话">
          <p className="nav-label">最近</p>
          <div className="history-list">
            {chats.length ? chats.map((chat) => (
              <button
                className={`history-item ${chat.id === activeId ? "active" : ""}`}
                type="button"
                key={chat.id}
                onClick={() => onOpenChat(chat.id)}
              >
                <MessageSquareText size={14} />
                <span>{chat.title}</span>
              </button>
            )) : <p className="empty-history">你的最近对话会显示在这里</p>}
          </div>
        </nav>

        <div className="sidebar-footer">
          <div className="knowledge-state">
            <span className="state-icon"><ShieldCheck size={15} /></span>
            <span><strong>企业知识库</strong><small>仅使用已批准文档</small></span>
          </div>
          <button className="footer-action" type="button" onClick={onToggleTheme}>
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            <span>{theme === "dark" ? "浅色外观" : "深色外观"}</span>
          </button>
        </div>
      </aside>
      {open && <button className="sidebar-scrim" type="button" onClick={onClose} aria-label="关闭侧栏" />}
    </>
  );
}

export function EmptyState({ onPrompt }) {
  /** 展示新会话引导，并把示例问题填入编辑器而非直接发送。 */
  return (
    <section className="empty-state">
      <div className="assistant-orb"><Sparkles size={18} /><span>T</span></div>
      <p className="eyebrow">TELLERX KNOWLEDGE</p>
      <h1>今天想了解什么？</h1>
      <p className="intro">查业务规则、核对接口定义，或从项目文档中找到有出处的答案。</p>

      <div className="starter-grid">
        {STARTERS.map(({ icon: Icon, title, description, prompt }) => (
          <button type="button" key={title} onClick={() => onPrompt(prompt)}>
            <span className="starter-icon"><Icon size={16} /></span>
            <span className="starter-copy"><strong>{title}</strong><small>{description}</small></span>
            <ChevronRight className="starter-arrow" size={16} />
          </button>
        ))}
      </div>
    </section>
  );
}

export function Composer({ value, onChange, onSubmit, disabled, scope }) {
  /** 自动调整输入框高度，并处理 Enter 发送与输入法组合态。 */
  const textareaRef = useRef(null);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [value]);

  function onKeyDown(event) {
    // 中文输入法合成过程中按 Enter 只用于选词，不能误触发送。
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      onSubmit(event);
    }
  }

  return (
    <div className="composer-area">
      <form className="composer" onSubmit={onSubmit}>
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={onKeyDown}
          rows="1"
          maxLength="4000"
          placeholder="向 TellerX 提问"
          aria-label="输入问题"
        />
        <div className="composer-footer">
          <div className="context-label"><Search size={13} /><span>{scope}</span></div>
          <button className="send-button" type="submit" disabled={disabled || !value.trim()} aria-label="发送消息">
            <ArrowUp size={17} strokeWidth={2.5} />
          </button>
        </div>
      </form>
      <p className="disclaimer">TellerX 可能会出错。重要信息请以引用的原始文档为准。</p>
    </div>
  );
}
