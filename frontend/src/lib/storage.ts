import type { ChatMessage } from '../api/types';
import { isChatResponse, isRecord } from '../api/validate';

export const SESSION_KEY = 'ecomrag.session_id';
export const MESSAGES_KEY = 'ecomrag.messages';
export const TRACE_KEY = 'ecomrag.trace';
export const CONVERSATIONS_KEY = 'lingma.conversations.v1';

export interface Conversation {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  messages: ChatMessage[];
}

let memory: Conversation[] = [];
let volatile = false;
let memorySession: string | undefined;
let volatileSession = false;
let legacyRead = false;

/** Reset module memory between isolated browser/test lifetimes. */
export function resetStorageMemory(): void {
  memory = [];
  volatile = false;
  memorySession = undefined;
  volatileSession = false;
  legacyRead = false;
}

export function setSessionId(id: string): void {
  memorySession = id;
  try { localStorage.setItem(SESSION_KEY, id); volatileSession = false; } catch { volatileSession = true; }
}

export function createId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  // randomUUID is not exposed by some browsers on non-secure LAN dev origins.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function newSessionId(): string {
  const id = createId();
  setSessionId(id);
  return id;
}

export function getSessionId(): string {
  if (volatileSession && memorySession) return memorySession;
  try {
    const id = localStorage.getItem(SESSION_KEY);
    if (id && id.trim() && Array.from(id).length <= 128) { memorySession = id; return id; }
  } catch { return memorySession || newSessionId(); }
  return newSessionId();
}

export function debugEnabled(): boolean {
  if (new URLSearchParams(window.location.search).get('debug') === '1') return true;
  try { return localStorage.getItem('ecomrag.debug') === '1'; } catch { return false; }
}

export function traceToggleVisible(): boolean {
  return import.meta.env.VITE_SHOW_TRACE_TOGGLE === '1' || debugEnabled();
}

export function loadTraceEnabled(): boolean {
  try { return sessionStorage.getItem(TRACE_KEY) === '1'; } catch { return false; }
}

export function saveTraceEnabled(enabled: boolean): void {
  try { sessionStorage.setItem(TRACE_KEY, enabled ? '1' : '0'); } catch { /* Keep the React state usable. */ }
}

function isMessage(value: unknown): value is ChatMessage {
  if (!isRecord(value) || typeof value.id !== 'string') return false;
  if (value.kind === 'user') return typeof value.text === 'string';
  if (typeof value.question !== 'string') return false;
  if (value.kind === 'error') return typeof value.code === 'number'
    && (value.requestId === undefined || typeof value.requestId === 'string');
  return value.kind === 'assistant' && isChatResponse(value.response)
    && typeof value.elapsedMs === 'number' && Number.isFinite(value.elapsedMs) && value.elapsedMs >= 0;
}

/** Keep adjacent, completed turns; discard orphan messages and interrupted questions. */
export function completedMessages(messages: ChatMessage[]): ChatMessage[] {
  const complete: ChatMessage[] = [];
  for (let i = 0; i < messages.length - 1; i += 1) {
    const user = messages[i];
    const reply = messages[i + 1];
    if (user.kind === 'user' && reply.kind !== 'user' && reply.question === user.text) {
      complete.push(user, reply);
      i += 1;
    }
  }
  return complete;
}

function normalize(conversations: Conversation[]): Conversation[] {
  const ids = new Set<string>();
  return conversations.map(item => ({ ...item, messages: completedMessages(item.messages).slice(-100) }))
    .filter(item => item.messages.length > 0)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .filter(item => { if (ids.has(item.id)) return false; ids.add(item.id); return true; })
    .slice(0, 30);
}

function isConversation(value: unknown): value is Conversation {
  return isRecord(value) && typeof value.id === 'string' && !!value.id.trim() && Array.from(value.id).length <= 128
    && typeof value.title === 'string' && Array.from(value.title).length <= 20
    && typeof value.createdAt === 'number' && Number.isFinite(new Date(value.createdAt).getTime()) && value.createdAt >= 0
    && typeof value.updatedAt === 'number' && Number.isFinite(new Date(value.updatedAt).getTime()) && value.updatedAt >= value.createdAt
    && Array.isArray(value.messages) && value.messages.every(isMessage);
}

export function saveConversations(conversations: Conversation[]): Conversation[] {
  memory = normalize(conversations);
  try { localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(memory)); volatile = false; }
  catch { volatile = true; /* Keep the entire list usable even after quota failures. */ }
  return memory;
}

export function loadConversations(now = Date.now()): Conversation[] {
  if (!volatile) {
    try {
      const raw: unknown = JSON.parse(localStorage.getItem(CONVERSATIONS_KEY) || '[]');
      memory = Array.isArray(raw) ? normalize(raw.filter(isConversation)) : [];
    } catch { /* Keep the last in-memory snapshot on blocked storage or malformed JSON. */ }
  }
  if (legacyRead) return memory;
  legacyRead = true;
  try {
    const legacy: unknown = JSON.parse(sessionStorage.getItem(MESSAGES_KEY) || 'null');
    if (isRecord(legacy) && typeof legacy.session_id === 'string'
      && legacy.session_id.trim() && Array.from(legacy.session_id).length <= 128
      && Array.isArray(legacy.messages) && legacy.messages.every(isMessage)) {
      const messages = completedMessages(legacy.messages);
      if (messages.length && !memory.some(item => item.id === legacy.session_id)) {
        saveConversations([...memory, { id: legacy.session_id, title: titleFor(messages), createdAt: now, updatedAt: now, messages }]);
        try { if (!localStorage.getItem(SESSION_KEY)) setSessionId(legacy.session_id); } catch { /* Keep current memory selection. */ }
      }
    }
    sessionStorage.removeItem(MESSAGES_KEY);
  } catch { /* Legacy storage is optional. */ }
  return memory;
}

function titleFor(messages: ChatMessage[]): string {
  const first = messages.find(message => message.kind === 'user');
  return first?.kind === 'user' ? Array.from(first.text).slice(0, 20).join('') : '新对话';
}

export function loadMessages(sessionId: string): ChatMessage[] {
  return loadConversations().find(item => item.id === sessionId)?.messages || [];
}

export function saveMessages(sessionId: string, messages: ChatMessage[], now = Date.now()): Conversation[] {
  const conversations = loadConversations(now);
  const complete = completedMessages(messages);
  if (!complete.length) return conversations;
  const existing = conversations.find(item => item.id === sessionId);
  return saveConversations([...conversations.filter(item => item.id !== sessionId), {
    id: sessionId, title: existing?.title || titleFor(complete), createdAt: existing?.createdAt ?? now,
    updatedAt: Math.max(now, existing?.createdAt ?? now), messages: complete,
  }]);
}

export function clearMessages(): void {
  legacyRead = true;
  saveConversations([]);
  try { sessionStorage.removeItem(MESSAGES_KEY); } catch { /* Continue in memory. */ }
}
