import { StrictMode, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Archive,
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clipboard,
  Code2,
  FileSearch,
  Menu,
  MessageSquareText,
  Moon,
  MoreHorizontal,
  PanelLeftClose,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Sun,
  ThumbsDown,
  ThumbsUp,
  X,
} from "lucide-react";
import "./styles.css";

const STORAGE_KEY = "tellerx-react-conversations-v1";
const THEME_KEY = "tellerx-theme";

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
  grounded: { label: "有据可查", className: "grounded" },
  conflict: { label: "存在冲突", className: "conflict" },
  insufficient_evidence: { label: "证据不足", className: "insufficient" },
};

function uid() {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

function getSavedChats() {
  try {
    const chats = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(chats) ? chats : [];
  } catch {
    return [];
  }
}

function saveChats(chats) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(chats.slice(0, 24)));
}

function SourceList({ sources }) {
  const [open, setOpen] = useState(false);
  if (!sources?.length) return null;

  return (
    <div className={`sources ${open ? "is-open" : ""}`}>
      <button className="sources-trigger" type="button" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <span><Archive size={14} /> 原文证据 · {sources.length} 条</span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="source-list">
          {sources.map((source, index) => {
            const location = [
              source.heading_path,
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
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function AnswerActions({ answer, onToast }) {
  const [vote, setVote] = useState(null);

  async function copyAnswer() {
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

function Message({ message, onToast }) {
  if (message.role === "user") {
    return (
      <article className="message user-message">
        <div className="user-bubble">{message.content}</div>
      </article>
    );
  }

  const status = STATUS[message.status] || STATUS.grounded;
  return (
    <article className="message assistant-message">
      <div className="assistant-avatar"><span>T</span></div>
      <div className="assistant-content">
        <div className="answer-copy">{message.content}</div>
        <SourceList sources={message.sources} />
        <div className="answer-meta">
          <span className={`answer-status ${status.className}`}><i />{status.label}</span>
          <span>{message.modelId ? `${message.modelId}${message.routeTier ? ` · ${message.routeTier}` : ""}` : "未调用生成模型"}</span>
        </div>
        <AnswerActions answer={message.content} onToast={onToast} />
      </div>
    </article>
  );
}

function Sidebar({ open, onClose, chats, activeId, onNew, onOpenChat, theme, onToggleTheme }) {
  return (
    <>
      <aside className={`sidebar ${open ? "open" : ""}`} aria-label="对话导航">
        <div className="sidebar-header">
          <a className="brand" href="/" aria-label="TellerX 首页">
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

function EmptyState({ onPrompt }) {
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

function Composer({ value, onChange, onSubmit, disabled, scope }) {
  const textareaRef = useRef(null);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [value]);

  function onKeyDown(event) {
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

function App() {
  const [theme, setTheme] = useState(() => localStorage.getItem(THEME_KEY) || "light");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState("");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState([]);
  const [sending, setSending] = useState(false);
  const [conversationId, setConversationId] = useState(null);
  const [localChatId, setLocalChatId] = useState(uid);
  const [chats, setChats] = useState(getSavedChats);
  const [toast, setToast] = useState("");
  const scrollRef = useRef(null);
  const toastTimer = useRef(null);

  const title = useMemo(() => {
    const first = messages.find((message) => message.role === "user")?.content;
    return first ? first.slice(0, 28) : "新对话";
  }, [messages]);
  const selectedProject = projects.find((item) => item.id === projectId);
  const scope = selectedProject ? `仅检索：${selectedProject.name}` : "检索全部项目";

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  useEffect(() => {
    fetch("/api/v1/projects")
      .then((response) => response.ok ? response.json() : [])
      .then(setProjects)
      .catch(() => setProjects([]));
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, sending]);

  useEffect(() => {
    function shortcut(event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        newChat();
      }
      if (event.key === "Escape") setSidebarOpen(false);
    }
    document.addEventListener("keydown", shortcut);
    return () => document.removeEventListener("keydown", shortcut);
  });

  function showToast(message) {
    setToast(message);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 1800);
  }

  function persist(nextMessages, nextConversationId = conversationId) {
    if (!nextMessages.length) return;
    const firstQuestion = nextMessages.find((message) => message.role === "user")?.content || "新对话";
    const nextChat = {
      id: localChatId,
      conversationId: nextConversationId,
      title: firstQuestion.slice(0, 28),
      messages: nextMessages,
      updatedAt: Date.now(),
    };
    const nextChats = [nextChat, ...chats.filter((chat) => chat.id !== localChatId)].slice(0, 24);
    setChats(nextChats);
    saveChats(nextChats);
  }

  function newChat() {
    setMessages([]);
    setConversationId(null);
    setLocalChatId(uid());
    setInput("");
    setSidebarOpen(false);
  }

  function openChat(id) {
    const chat = chats.find((item) => item.id === id);
    if (!chat) return;
    setLocalChatId(chat.id);
    setConversationId(chat.conversationId || null);
    setMessages(chat.messages || []);
    setSidebarOpen(false);
  }

  function clearChat() {
    if (!messages.length) return;
    const nextChats = chats.filter((chat) => chat.id !== localChatId);
    setChats(nextChats);
    saveChats(nextChats);
    newChat();
    showToast("当前对话已清空");
  }

  async function submit(event) {
    event?.preventDefault?.();
    const question = input.trim();
    if (!question || sending) return;
    const userMessage = { id: uid(), role: "user", content: question };
    const pendingMessages = [...messages, userMessage];
    setMessages(pendingMessages);
    setInput("");
    setSending(true);

    try {
      const response = await fetch("/api/v1/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          conversation_id: conversationId,
          project_ids: projectId ? [projectId] : [],
        }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `请求失败（${response.status}）`);
      }
      const data = await response.json();
      const answer = {
        id: uid(),
        role: "assistant",
        content: data.answer,
        status: data.status,
        sources: data.sources || [],
        modelId: data.model_id,
        routeTier: data.route_tier,
      };
      const completedMessages = [...pendingMessages, answer];
      setConversationId(data.conversation_id);
      setMessages(completedMessages);
      persist(completedMessages, data.conversation_id);
    } catch (error) {
      const failedMessages = [...pendingMessages, {
        id: uid(),
        role: "assistant",
        content: `暂时无法完成请求。${error.message}`,
        status: "insufficient_evidence",
        sources: [],
      }];
      setMessages(failedMessages);
      persist(failedMessages);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="app-shell">
      <Sidebar
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        chats={chats}
        activeId={localChatId}
        onNew={newChat}
        onOpenChat={openChat}
        theme={theme}
        onToggleTheme={() => setTheme((value) => value === "dark" ? "light" : "dark")}
      />

      <main className="workspace">
        <header className="topbar">
          <button className="icon-button menu-button" type="button" onClick={() => setSidebarOpen(true)} aria-label="打开侧栏"><Menu size={19} /></button>
          <button className="conversation-title" type="button" title={title}>{title}<ChevronDown size={13} /></button>
          <div className="topbar-actions">
            <label className="project-picker">
              <span>知识范围</span>
              <select value={projectId} onChange={(event) => setProjectId(event.target.value)} aria-label="选择知识库项目">
                <option value="">全部项目</option>
                {projects.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
              </select>
            </label>
            <button className="icon-button" type="button" onClick={clearChat} aria-label="清空当前对话" title="清空当前对话"><MoreHorizontal size={19} /></button>
          </div>
        </header>

        <section className="chat-scroll" ref={scrollRef} aria-live="polite">
          {!messages.length && !sending ? <EmptyState onPrompt={(prompt) => setInput(prompt)} /> : (
            <div className="messages">
              {messages.map((message) => <Message message={message} onToast={showToast} key={message.id} />)}
              {sending && (
                <article className="message assistant-message" aria-label="正在查找答案">
                  <div className="assistant-avatar"><span>T</span></div>
                  <div className="thinking"><i /><i /><i /></div>
                </article>
              )}
            </div>
          )}
        </section>

        <Composer value={input} onChange={setInput} onSubmit={submit} disabled={sending} scope={scope} />
      </main>
      <div className={`toast ${toast ? "show" : ""}`} role="status">{toast}<Check size={14} /></div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(
  <StrictMode><App /></StrictMode>
);
