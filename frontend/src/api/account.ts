import { apiJSON } from './http';
import { ChatError } from './errors';
import type { ChatResponse, Route, Source } from './types';

export interface User { id: string | number; username: string }
export interface Quota { role: 'guest' | 'user'; limit: number; used: number; remaining: number; resets_at: string }
export interface Identity { user: User | null; quota: Quota }
export interface ConversationSummary { id: string; title: string; updated_at: string; message_count: number }
export interface ConversationDetail { id: string; title: string; messages: {
  role: 'user' | 'assistant'; content: string; route?: Route | null; sources?: Source[] | null; elapsed_ms?: number | null; created_at: string;
}[] }

export const accountAPI = {
  me: (signal?: AbortSignal) => apiJSON<Identity>('/auth/me', 'GET', undefined, signal),
  authenticate: (mode: 'login' | 'register', username: string, password: string) =>
    apiJSON<{ user: User }>(`/auth/${mode}`, 'POST', { username, password }),
  logout: () => apiJSON<void>('/auth/logout', 'POST'),
  remove: (password: string) => apiJSON<void>('/auth/account', 'DELETE', { password }),
};
export const conversationsAPI = {
  list: (signal?: AbortSignal) => apiJSON<{ items: ConversationSummary[] }>('/conversations', 'GET', undefined, signal),
  create: (signal?: AbortSignal) => apiJSON<ConversationSummary>('/conversations', 'POST', {}, signal),
  open: (id: string, signal?: AbortSignal) => apiJSON<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`, 'GET', undefined, signal),
  rename: (id: string, title: string) => apiJSON<void>(`/conversations/${encodeURIComponent(id)}`, 'PATCH', { title }),
  remove: (id: string) => apiJSON<void>(`/conversations/${encodeURIComponent(id)}`, 'DELETE'),
  saveAnswer: (id: string, response: ChatResponse, elapsedMs: number) =>
    saveAnswerWithRetry(id, {
      request_id: response.request_id,
      // Match the server's Unicode character limits before sending the completed answer.
      content: Array.from(response.answer).slice(0, 4000).join(''), route: response.route,
      sources: response.sources.slice(0, 8).map(source => ({ index: source.index, score: source.score,
        doc_id: Array.from(source.doc_id).slice(0, 128).join(''),
        title: Array.from(source.title).slice(0, 200).join(''), snippet: Array.from(source.snippet).slice(0, 500).join(''),
      })),
      ...(Number.isInteger(elapsedMs) && elapsedMs >= 0 && elapsedMs <= 3_600_000 ? { elapsed_ms: elapsedMs } : {}),
    }),
};

async function saveAnswerWithRetry(id: string, body: Record<string, unknown>): Promise<void> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      // The backend merges writes by (conversation_id, request_id) under a row lock.
      // Reuse the exact payload even if the server saved it before a response was lost.
      await apiJSON<void>(`/conversations/${encodeURIComponent(id)}/messages`, 'POST', body);
      return;
    } catch (error) {
      const transient = error instanceof ChatError && (error.status !== undefined
        ? error.status >= 500 && error.status <= 599 : error.code >= 50000 && error.code < 60000);
      if (!transient || attempt >= 2) throw error;
      await new Promise<void>(resolve => window.setTimeout(resolve, 500 * 2 ** attempt));
    }
  }
}

export function guestIdentity(): Identity {
  return { user: null, quota: { role: 'guest', limit: 5, used: 0, remaining: 5, resets_at: '' } };
}

export function accountError(error: unknown): string {
  if (!(error instanceof Error)) return '服务暂不可用，请稍后再试。';
  const code = 'code' in error ? error.code : undefined;
  const messages: Record<number, string> = {
    40001: '请检查用户名和密码。', 40002: '用户名须为 3–20 位字母、数字或下划线。',
    40003: '密码须为 8–64 位，不能与用户名相同、为纯数字或常见弱口令。',
    40101: '登录已失效，请重新登录。', 40102: '账号不存在。', 40103: '密码错误。',
    40301: '访问来源未被允许，请使用正确的访问地址。', 40401: '对话不存在，请刷新历史列表。',
    40901: '用户名已被使用。', 42903: '请求过多或账号暂时锁定，请稍后再试。',
    50301: '服务暂不可用，请稍后再试。',
  };
  // Keep precise password-strength and lockout reasons supplied by the server.
  if (Number(code) !== 40301 && 'reason' in error && typeof error.reason === 'string' && error.reason) return error.reason;
  return messages[Number(code)] || '服务暂不可用，请稍后再试。';
}
