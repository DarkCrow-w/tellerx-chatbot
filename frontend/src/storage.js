/** Versioned local persistence. Server-side messages remain authoritative. */

const CONVERSATIONS_KEY = "tellerx-react-conversations-v1";
const THEME_KEY = "tellerx-theme";
const MAX_SAVED_CHATS = 24;

export function uid() {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

export function getSavedChats() {
  try {
    const chats = JSON.parse(localStorage.getItem(CONVERSATIONS_KEY) || "[]");
    return Array.isArray(chats) ? chats : [];
  } catch {
    return [];
  }
}

export function saveChats(chats) {
  localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(chats.slice(0, MAX_SAVED_CHATS)));
}

export function getSavedTheme() {
  return localStorage.getItem(THEME_KEY) || "light";
}

export function saveTheme(theme) {
  localStorage.setItem(THEME_KEY, theme);
}

