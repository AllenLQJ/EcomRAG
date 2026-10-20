import type { ChatMessage } from '../api/types';
import { isChatResponse, isRecord } from '../api/validate';
import { withProductCards } from '../api/products';

export const SESSION_KEY = 'ecomrag.session_id';
export const MESSAGES_KEY = 'ecomrag.messages';
export const TRACE_KEY = 'ecomrag.trace';
export const CONVERSATIONS_KEY = 'lingma.conversations.v1';
// Bound the raw JS string before parsing, including legacy snapshots.
export const MAX_STORAGE_LENGTH = 2 * 1024 * 1024;

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
  if (value.kind === 'error') return typeof value.code === 'number' && Number.isFinite(value.code)
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
    if (user?.kind === 'user' && reply && reply.kind !== 'user' && reply.question === user.text) {
      complete.push(user, reply);
      i += 1;
    }
  }
  return complete;
}

interface StoredConversation {
  id: string;
  title?: unknown;
  createdAt: number;
  updatedAt: number;
  messages: unknown[];
}

/** Message validity is checked individually, never as a condition on the whole session. */
function isConversation(value: unknown): value is StoredConversation {
  return isRecord(value) && typeof value.id === 'string' && !!value.id.trim() && Array.from(value.id).length <= 128
    && typeof value.createdAt === 'number' && Number.isFinite(new Date(value.createdAt).getTime()) && value.createdAt >= 0
    && typeof value.updatedAt === 'number' && Number.isFinite(new Date(value.updatedAt).getTime()) && value.updatedAt >= value.createdAt
    && Array.isArray(value.messages);
}

// Storage limits use string length (UTF-16 code units), without splitting surrogate pairs.
function truncate(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const end = /[\uD800-\uDBFF]/.test(text[limit - 1]) ? limit - 1 : limit;
  return text.slice(0, end);
}

function limitMessage(message: ChatMessage): ChatMessage {
  if (message.kind === 'user') return { ...message, text: truncate(message.text, 4000) };
  const question = truncate(message.question, 4000);
  if (message.kind === 'error') return { ...message, question };
  const response = { ...withProductCards(message.response),
    answer: truncate(message.response.answer, 8000),
    sources: message.response.sources.map(source => ({ ...source,
      title: truncate(source.title, 40), snippet: truncate(source.snippet, 500),
    })),
  };
  const limited = { ...message, question, response };
  if (response.products) {
    // Optional cards may use only the space left in a 32 KiB local message.
    // Preserve existing text/history behavior if the message already exceeds it.
    try {
      while (response.products.length && new TextEncoder().encode(JSON.stringify(limited)).byteLength > 32 * 1024) response.products.pop();
    } catch { response.products = []; }
    if (!response.products.length) delete response.products;
  }
  return limited;
}

function normalize(conversations: unknown[]): Conversation[] {
  const ids = new Set<string>();
  return conversations.filter(isConversation).map(item => {
    const messages = completedMessages(item.messages.filter(isMessage)).slice(-100);
    return { id: item.id, createdAt: item.createdAt, updatedAt: item.updatedAt,
      title: typeof item.title === 'string' ? truncate(item.title, 40) : titleFor(messages),
      messages: messages.map(limitMessage),
    };
  }).filter(item => item.messages.length > 0)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .filter(item => { if (ids.has(item.id)) return false; ids.add(item.id); return true; })
    .slice(0, 30);
}

function readJSON(storage: Storage, key: string, fallback: unknown): unknown {
  const raw = storage.getItem(key);
  if (!raw) return fallback;
  // Never pass oversized input to JSON.parse, even when clearing the key is blocked.
  if (raw.length <= MAX_STORAGE_LENGTH) {
    try { return JSON.parse(raw) as unknown; } catch { /* Treat malformed JSON as corrupt. */ }
  }
  try { storage.removeItem(key); } catch { /* The corrupt snapshot is still ignored. */ }
  return fallback;
}

export function saveConversations(conversations: Conversation[]): Conversation[] {
  memory = normalize(conversations);
  try {
    const serialized = JSON.stringify(memory);
    // Do not persist a snapshot that the next load must reject. Keep it in memory instead.
    if (serialized.length > MAX_STORAGE_LENGTH) { volatile = true; return memory; }
    localStorage.setItem(CONVERSATIONS_KEY, serialized);
    volatile = false;
  } catch { volatile = true; /* Keep the entire list usable even after quota failures. */ }
  return memory;
}

export function loadConversations(now = Date.now()): Conversation[] {
  if (!volatile) {
    try {
      const raw = readJSON(localStorage, CONVERSATIONS_KEY, []);
      memory = Array.isArray(raw) ? normalize(raw) : [];
    } catch { /* Keep the last in-memory snapshot on blocked storage or malformed JSON. */ }
  }
  if (legacyRead) return memory;
  legacyRead = true;
  try {
    const legacy = readJSON(sessionStorage, MESSAGES_KEY, null);
    if (isRecord(legacy) && typeof legacy.session_id === 'string'
      && legacy.session_id.trim() && Array.from(legacy.session_id).length <= 128
      && Array.isArray(legacy.messages)) {
      const messages = completedMessages(legacy.messages.filter(isMessage));
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
  const complete = completedMessages(messages.filter(isMessage));
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
