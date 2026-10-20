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
  if (error instanceof Object && 'reason' in error && typeof error.reason === 'string' && error.reason) return error.reason;
  return ({ 40001: '请检查用户名和密码。', 40101: '用户名或密码错误，或登录已失效。',
    40301: '请求被拒绝，请刷新后重试。', 40401: '对话不存在，请刷新历史列表。',
    40901: '用户名已被使用。', 42903: '登录尝试过多，请稍后再试。',
    50301: '服务暂不可用，请稍后再试。' } as Record<number, string>)[Number(code)] || '服务暂不可用，请稍后再试。';
}
