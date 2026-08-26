/** Versioned local persistence. Server-side messages remain authoritative. */

const CONVERSATIONS_KEY = "tellerx-react-conversations-v1";
const THEME_KEY = "tellerx-theme";
const MAX_SAVED_CHATS = 24;

export function uid() {
  /** 生成仅用于前端渲染和本地草稿的标识，不作为服务端业务主键。 */
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

export function getSavedChats() {
  /** 读取版本化的本地对话快照；损坏数据按空列表处理。 */
  try {
    const chats = JSON.parse(localStorage.getItem(CONVERSATIONS_KEY) || "[]");
    return Array.isArray(chats) ? chats : [];
  } catch {
    return [];
  }
}

export function saveChats(chats) {
  /** 限量保存最近对话，避免 localStorage 无界增长。 */
  localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(chats.slice(0, MAX_SAVED_CHATS)));
}

export function getSavedTheme() {
  /** 返回已保存主题，首次访问默认使用浅色。 */
  return localStorage.getItem(THEME_KEY) || "light";
}

export function saveTheme(theme) {
  /** 持久化用户的主题偏好。 */
  localStorage.setItem(THEME_KEY, theme);
}
