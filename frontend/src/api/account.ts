import { apiJSON } from './http';
import type { Route, Source } from './types';

export interface User { id: string | number; username: string }
export interface Quota { role: 'guest' | 'user'; limit: number; used: number; remaining: number; resets_at: string }
export interface Identity { user: User | null; quota: Quota }
export interface ConversationSummary { id: string; title: string; updated_at: string; message_count: number }
export interface ConversationDetail { id: string; title: string; messages: {
  role: 'user' | 'assistant'; content: string; route?: Route | null; sources?: Source[] | null; created_at: string;
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
};

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
