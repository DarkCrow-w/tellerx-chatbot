import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Menu, MoreHorizontal } from "lucide-react";

import { askKnowledgeBaseStream, listProjects } from "./api";
import { Composer, EmptyState, Message, Sidebar } from "./components";
import KnowledgeManager from "./KnowledgeManager";
import AnswerProgress from "./AnswerProgress";
import { getSavedChats, getSavedTheme, saveChats, saveTheme, uid } from "./storage";

function viewFromHash() {
  return globalThis.location.hash === "#/knowledge" ? "knowledge" : "chat";
}

function ChatMessages({ messages, sending, progressEvents, startedAt, onPrompt, onToast, onSelectDocument, onStop }) {
  if (!messages.length && !sending) return <EmptyState onPrompt={onPrompt} />;
  return (
    <div className="messages">
      {messages.map((message) => (
        <div key={message.id}>
          {message.progress && <div className="saved-progress"><AnswerProgress {...message.progress} /></div>}
          <Message
            message={message}
            onToast={onToast}
            onSelectDocument={onSelectDocument}
          />
        </div>
      ))}
      {sending && (
        <article className="message assistant-message" aria-label="正在查找答案">
          <div className="assistant-avatar"><span>T</span></div>
          <AnswerProgress events={progressEvents} startedAt={startedAt} active onStop={onStop} />
        </article>
      )}
    </div>
  );
}

function ChatWorkspace({
  title, projects, projectId, scope, messages, sending, progressEvents,
  startedAt, scrollRef, input, onOpenSidebar, onProjectChange, onClear,
  onInputChange, onSubmit, onToast, onSelectDocument, onStop,
}) {
  return (
    <main className="workspace">
      <header className="topbar">
        <button className="icon-button menu-button" type="button" onClick={onOpenSidebar} aria-label="打开侧栏"><Menu size={19} /></button>
        <button className="conversation-title" type="button" title={title}>{title}<ChevronDown size={13} /></button>
        <div className="topbar-actions">
          <label className="project-picker">
            <span>知识范围</span>
            <select value={projectId} onChange={(event) => onProjectChange(event.target.value)} aria-label="选择知识库项目">
              <option value="">全部知识库</option>
              {projects.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
            </select>
          </label>
          <button className="icon-button" type="button" onClick={onClear} aria-label="清空当前对话" title="清空当前对话"><MoreHorizontal size={19} /></button>
        </div>
      </header>

      <section className="chat-scroll" ref={scrollRef} aria-live="polite">
        <ChatMessages
          messages={messages}
          sending={sending}
          progressEvents={progressEvents}
          startedAt={startedAt}
          onPrompt={onInputChange}
          onToast={onToast}
          onSelectDocument={onSelectDocument}
          onStop={onStop}
        />
      </section>

      <Composer value={input} onChange={onInputChange} onSubmit={onSubmit} disabled={sending} scope={scope} />
    </main>
  );
}

/** 页面顶层状态与用例编排；纯视觉细节集中在 components.jsx。 */
export default function App() {
  const [theme, setTheme] = useState(getSavedTheme);
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
  const [view, setView] = useState(viewFromHash);
  const scrollRef = useRef(null);
  const toastTimer = useRef(null);
  const activeRequest = useRef(null);
  const [progressEvents, setProgressEvents] = useState([]);
  const [startedAt, setStartedAt] = useState(null);

  const abandonRequest = useCallback(() => {
    activeRequest.current?.abort();
    activeRequest.current = null;
    setSending(false);
    setProgressEvents([]);
  }, []);

  const title = useMemo(() => {
    const first = messages.find((message) => message.role === "user")?.content;
    return first ? first.slice(0, 28) : "新对话";
  }, [messages]);
  const selectedProject = projects.find((item) => item.id === projectId);
  const scope = selectedProject
    ? `仅检索：${selectedProject.name}`
    : "检索全部知识库";

  const navigate = useCallback((nextView) => {
    const hash = nextView === "knowledge" ? "#/knowledge" : "#/chat";
    if (globalThis.location.hash !== hash) globalThis.location.hash = hash;
    setView(nextView);
  }, []);

  const newChat = useCallback(() => {
    // 本地会话 ID 与服务端 conversationId 分离，新对话必须同时重置二者。
    abandonRequest();
    setMessages([]);
    setConversationId(null);
    setLocalChatId(uid());
    setInput("");
    setSidebarOpen(false);
    navigate("chat");
  }, [navigate, abandonRequest]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    saveTheme(theme);
  }, [theme]);

  const refreshProjects = useCallback(async (preferredId = null) => {
    const items = await listProjects();
    setProjects(items);
    setProjectId((current) => {
      if (preferredId && items.some((item) => item.id === preferredId)) return preferredId;
      if (items.some((item) => item.id === current)) return current;
      return items.length === 1 ? items[0].id : "";
    });
    return items;
  }, []);

  useEffect(() => {
    refreshProjects().catch(() => setProjects([]));
  }, [refreshProjects]);

  useEffect(() => {
    function onHashChange() {
      setView(viewFromHash());
      setSidebarOpen(false);
    }
    globalThis.addEventListener("hashchange", onHashChange);
    return () => globalThis.removeEventListener("hashchange", onHashChange);
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
  }, [newChat]);

  useEffect(() => () => {
    clearTimeout(toastTimer.current);
    activeRequest.current?.abort();
    activeRequest.current = null;
  }, []);

  const showToast = useCallback((message) => {
    /** 显示短暂提示，并覆盖尚未结束的上一次计时器。 */
    setToast(message);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 1800);
  }, []);

  function persist(nextMessages, nextConversationId = conversationId) {
    /** 把当前服务端会话的前端快照提升到最近列表首位。 */
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

  function openChat(id) {
    /** 恢复本地消息及对应服务端会话 ID，确保后续提问延续上下文。 */
    const chat = chats.find((item) => item.id === id);
    if (!chat) return;
    abandonRequest();
    setLocalChatId(chat.id);
    setConversationId(chat.conversationId || null);
    setMessages(chat.messages || []);
    setSidebarOpen(false);
    navigate("chat");
  }

  function clearChat() {
    /** 只清理浏览器中的当前快照，不删除服务端审计消息。 */
    if (!messages.length) return;
    const nextChats = chats.filter((chat) => chat.id !== localChatId);
    setChats(nextChats);
    saveChats(nextChats);
    newChat();
    showToast("当前对话已清空");
  }

  async function runQuestion(question, { documentId = null, visibleText = question } = {}) {
    /** 乐观加入用户选择或问题，并等待服务端返回经过证据校验的回答。 */
    if (!question || sending) return;
    const userMessage = { id: uid(), role: "user", content: visibleText };
    const pendingMessages = [...messages, userMessage];
    setMessages(pendingMessages);
    setInput("");
    setSending(true);
    const controller = new AbortController();
    activeRequest.current = controller;
    const start = Date.now();
    const events = [];
    setStartedAt(start);
    setProgressEvents([]);

    try {
      const data = await askKnowledgeBaseStream({
        question, conversationId, projectId, documentId, signal: controller.signal,
        onProgress: (event) => {
          if (activeRequest.current !== controller) return;
          events.push(event);
          setProgressEvents([...events]);
        },
      });
      if (activeRequest.current !== controller) return;
      const answer = {
        id: uid(),
        role: "assistant",
        content: data.answer,
        status: data.status,
        sources: data.sources || [],
        modelId: data.model_id,
        routeTier: data.route_tier,
        retrievalIntent: data.retrieval_intent,
        resolvedDocument: data.resolved_document,
        resolvedScope: data.resolved_scope,
        searchScope: selectedProject ? `知识库：${selectedProject.name}` : "全部知识库",
        clarificationOptions: data.clarification_options || [],
        originalQuestion: question,
        progress: { events, duration: Date.now() - start },
      };
      const completedMessages = [...pendingMessages, answer];
      setConversationId(data.conversation_id);
      setMessages(completedMessages);
      persist(completedMessages, data.conversation_id);
    } catch (error) {
      if (activeRequest.current !== controller) return;
      const failedMessages = [...pendingMessages, {
        id: uid(),
        role: "assistant",
        content: error.name === "AbortError" ? "已停止等待。" : `暂时无法完成请求。${error.message}`,
        progress: { events, duration: Date.now() - start, failed: true },
        status: "insufficient_evidence",
        sources: [],
      }];
      setMessages(failedMessages);
      persist(failedMessages);
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setSending(false);
      }
    }
  }

  async function submit(event) {
    /** 提交输入框中的自然语言问题。 */
    event?.preventDefault?.();
    const question = input.trim();
    if (!question) return;
    await runQuestion(question);
  }

  function selectDocument(question, option) {
    /** 用户确认候选后，用稳定文档 ID 重新执行原问题。 */
    runQuestion(question, {
      documentId: option.document_id,
      visibleText: `选择文档：${option.filename}`,
    });
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
        activeView={view}
        onManage={() => { navigate("knowledge"); setSidebarOpen(false); }}
        theme={theme}
        onToggleTheme={() => setTheme((value) => value === "dark" ? "light" : "dark")}
      />

      {view === "knowledge" ? (
        <KnowledgeManager
          projects={projects}
          projectId={projectId}
          onProjectChange={setProjectId}
          onProjectsChanged={refreshProjects}
          onOpenSidebar={() => setSidebarOpen(true)}
          onToast={showToast}
        />
      ) : <ChatWorkspace
        title={title} projects={projects} projectId={projectId} scope={scope}
        messages={messages} sending={sending} progressEvents={progressEvents}
        startedAt={startedAt} scrollRef={scrollRef} input={input}
        onOpenSidebar={() => setSidebarOpen(true)} onProjectChange={setProjectId}
        onClear={clearChat} onInputChange={setInput} onSubmit={submit}
        onToast={showToast} onSelectDocument={selectDocument}
        onStop={() => activeRequest.current?.abort()}
      />}
      <div className={`toast ${toast ? "show" : ""}`} role="status">{toast}<Check size={14} /></div>
    </div>
  );
}
