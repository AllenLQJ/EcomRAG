import type { ChatMessage } from '../api/types';
import { isChatResponse, isRecord } from '../api/validate';

export const SESSION_KEY = 'ecomrag.session_id';
export const MESSAGES_KEY = 'ecomrag.messages';

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
  try { localStorage.setItem(SESSION_KEY, id); } catch { /* Continue in memory. */ }
  return id;
}

export function getSessionId(): string {
  try {
    const id = localStorage.getItem(SESSION_KEY);
    if (id && id.trim() && Array.from(id).length <= 128) return id;
  } catch { /* Storage can be unavailable in private/embedded contexts. */ }
  return newSessionId();
}

export function debugEnabled(): boolean {
  if (new URLSearchParams(window.location.search).get('debug') === '1') return true;
  try { return localStorage.getItem('ecomrag.debug') === '1'; } catch { return false; }
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

export function loadMessages(sessionId: string): ChatMessage[] {
  try {
    const raw: unknown = JSON.parse(sessionStorage.getItem(MESSAGES_KEY) || 'null');
    if (!isRecord(raw) || raw.session_id !== sessionId || !Array.isArray(raw.messages)) return [];
    return raw.messages.every(isMessage) ? raw.messages : [];
  } catch { return []; }
}

export function saveMessages(sessionId: string, messages: ChatMessage[]): void {
  try { sessionStorage.setItem(MESSAGES_KEY, JSON.stringify({ session_id: sessionId, messages })); }
  catch { /* Quota, blocked storage: the current conversation still works. */ }
}

export function clearMessages(): void {
  try { sessionStorage.removeItem(MESSAGES_KEY); } catch { /* Continue in memory. */ }
}
