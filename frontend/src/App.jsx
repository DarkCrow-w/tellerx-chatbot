import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Menu, MoreHorizontal } from "lucide-react";

import { askKnowledgeBase, listProjects } from "./api";
import { Composer, EmptyState, Message, Sidebar } from "./components";
import KnowledgeManager from "./KnowledgeManager";
import { getSavedChats, getSavedTheme, saveChats, saveTheme, uid } from "./storage";

function viewFromHash() {
  return globalThis.location.hash === "#/knowledge" ? "knowledge" : "chat";
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
    setMessages([]);
    setConversationId(null);
    setLocalChatId(uid());
    setInput("");
    setSidebarOpen(false);
    navigate("chat");
  }, [navigate]);

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

  useEffect(() => () => clearTimeout(toastTimer.current), []);

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

    try {
      const data = await askKnowledgeBase({
        question, conversationId, projectId, documentId,
      });
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
        clarificationOptions: data.clarification_options || [],
        originalQuestion: question,
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
      ) : <main className="workspace">
        <header className="topbar">
          <button className="icon-button menu-button" type="button" onClick={() => setSidebarOpen(true)} aria-label="打开侧栏"><Menu size={19} /></button>
          <button className="conversation-title" type="button" title={title}>{title}<ChevronDown size={13} /></button>
          <div className="topbar-actions">
            <label className="project-picker">
              <span>知识范围</span>
              <select value={projectId} onChange={(event) => setProjectId(event.target.value)} aria-label="选择知识库项目">
                <option value="">全部知识库</option>
                {projects.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
              </select>
            </label>
            <button className="icon-button" type="button" onClick={clearChat} aria-label="清空当前对话" title="清空当前对话"><MoreHorizontal size={19} /></button>
          </div>
        </header>

        <section className="chat-scroll" ref={scrollRef} aria-live="polite">
          {!messages.length && !sending ? <EmptyState onPrompt={setInput} /> : (
            <div className="messages">
              {messages.map((message) => (
                <Message
                  message={message}
                  onToast={showToast}
                  onSelectDocument={selectDocument}
                  key={message.id}
                />
              ))}
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
      </main>}
      <div className={`toast ${toast ? "show" : ""}`} role="status">{toast}<Check size={14} /></div>
    </div>
  );
}
