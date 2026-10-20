import { vi } from 'vitest';
import { guestIdentity, type ConversationDetail, type ConversationSummary, type Identity } from '../api/account';
import { fixtureFor } from '../mocks/fixtures';
import type { ChatRequest } from '../api/types';

export const testUser = { id: 'user-1', username: 'demo_user' };
export function userIdentity(remaining = 50): Identity {
  return { user: testUser, quota: { role: 'user', limit: 50, used: 50 - remaining, remaining, resets_at: '2026-10-20T00:00:00Z' } };
}
export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

/** Offline-only transport; every unknown path fails, never calls the real fetch. */
export function accountMock(initial = guestIdentity()) {
  const state: { identity: Identity; items: ConversationSummary[]; details: Record<string, ConversationDetail> } = {
    identity: initial, items: [], details: {},
  };
  const overrides: Record<string, (init: RequestInit) => Response | Promise<Response>> = {};
  const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    const path = url.replace(/^.*\/api\/v1/, '');
    const method = init.method || 'GET';
    const override = overrides[`${method} ${path}`];
    if (override) return override(init);
    const body = init.body ? JSON.parse(String(init.body)) : {};
    if (path === '/auth/me') return json(state.identity);
    if (path === '/auth/login' || path === '/auth/register') {
      state.identity = userIdentity(); return json({ user: testUser }, path.endsWith('register') ? 201 : 200);
    }
    if (path === '/auth/logout' || path === '/auth/account') { state.identity = guestIdentity(); return json({}); }
    if (path === '/conversations' && method === 'GET') return json({ items: state.items });
    if (path === '/conversations' && method === 'POST') {
      const item = { id: `server-${state.items.length + 1}`, title: '新对话', updated_at: new Date().toISOString(), message_count: 0 };
      state.items.push(item); state.details[item.id] = { id: item.id, title: item.title, messages: [] };
      return json(item, 201);
    }
    if (path.startsWith('/conversations/')) {
      const id = decodeURIComponent(path.slice('/conversations/'.length));
      if (method === 'GET') return state.details[id] ? json(state.details[id]) : json({ code: 40401, message: '对话不存在' }, 404);
      if (method === 'PATCH') { state.items = state.items.map(item => item.id === id ? { ...item, title: body.title } : item); return json({}); }
      if (method === 'DELETE') { state.items = state.items.filter(item => item.id !== id); delete state.details[id]; return json({}); }
    }
    if (path === '/chat') {
      const request = body as ChatRequest;
      state.identity = { ...state.identity, quota: { ...state.identity.quota, used: state.identity.quota.used + 1, remaining: state.identity.quota.remaining - 1 } };
      const item = state.items.find(item => item.id === request.conversation_id);
      if (item) { item.title = request.message; item.message_count += 2; }
      return json(fixtureFor(request));
    }
    throw new Error(`Unexpected mock request: ${method} ${path}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { state, overrides, fetchMock,
    calls: (path: string, method?: string) => fetchMock.mock.calls.filter(([url, init]) => url.endsWith(`/api/v1${path}`) && (!method || (init?.method || 'GET') === method)),
  };
}
