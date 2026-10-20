import { describe, expect, it, vi } from 'vitest';
import { accountAPI, conversationsAPI } from './account';
import { sendChat, sendChatStream } from './client';
import { accountMock, json, userIdentity } from '../test/account';
import { controlledSSE } from '../test/streams';
import { fixtureFor } from '../mocks/fixtures';

const request = { session_id: 's', message: '你好', debug: false, conversation_id: 'server-id' };

describe('shared account/history transport policy', () => {
  it('adds X-Requested-With only to account/history writes and includes Cookie policy everywhere', async () => {
    const mock = accountMock(userIdentity());
    await accountAPI.me();
    await accountAPI.authenticate('register', 'demo_123', 'demo_pass_123');
    await accountAPI.authenticate('login', 'demo_123', 'demo_pass_123');
    await conversationsAPI.list();
    const created = await conversationsAPI.create();
    await conversationsAPI.open(created.id);
    await conversationsAPI.rename(created.id, '新标题');
    await conversationsAPI.remove(created.id);
    await accountAPI.logout();
    await accountAPI.remove('demo_pass_123');
    await sendChat(request);
    const stream = controlledSSE();
    stream.event({ event: 'done', data: fixtureFor(request) });
    mock.overrides['POST /chat/stream'] = () => stream.response;
    await sendChatStream(request, vi.fn());
    for (const [url, init] of mock.fetchMock.mock.calls) {
      expect(init?.credentials).toBe('same-origin');
      const mutation = ['POST', 'PATCH', 'DELETE'].includes(init?.method || 'GET');
      expect(new Headers(init?.headers).get('X-Requested-With')).toBe(mutation && !url.includes('/chat') ? 'ecomrag' : null);
    }
    for (const path of ['/chat', '/chat/stream']) expect(JSON.parse(String(mock.calls(path)[0][1]?.body))).toHaveProperty('conversation_id', 'server-id');
    expect(mock.calls('/auth/account')[0][1]?.method).toBe('DELETE');
  });
  it('joins the configured API base for auth and history without changing Cookie policy', async () => {
    vi.stubEnv('VITE_API_BASE', 'https://api.example.test/');
    const mock = accountMock(userIdentity()); await accountAPI.me(); await conversationsAPI.list();
    expect(mock.fetchMock.mock.calls.map(([url]) => url)).toEqual([
      'https://api.example.test/api/v1/auth/me', 'https://api.example.test/api/v1/conversations',
    ]);
  });
  it.each([40001, 40101, 40301, 40401, 40901, 42903, 50301])('preserves envelope fields for business error %s', async code => {
    const mock = accountMock();
    mock.overrides['POST /auth/login'] = () => json({ code, message: '可展示的中文原因', retry_after_seconds: 7 }, 400);
    await expect(accountAPI.authenticate('login', 'demo_123', 'demo_pass_123')).rejects.toMatchObject({ code, reason: '可展示的中文原因', retryAfterSeconds: 7 });
  });
  it.each(['json', 'http-sse', 'event-sse'])('retains quota scope through %s and never retries a business rejection', async transport => {
    const mock = accountMock();
    const body = { code: 42902, message: '今日次数用完', limit_scope: 'guest' as const };
    mock.overrides['POST /chat'] = () => json(body, 429);
    mock.overrides['POST /chat/stream'] = () => json(body, 429);
    if (transport === 'event-sse') {
      const stream = controlledSSE(); stream.event({ event: 'error', data: body });
      mock.overrides['POST /chat/stream'] = () => stream.response;
    }
    await expect(transport === 'json' ? sendChat(request) : sendChatStream(request, vi.fn())).rejects.toMatchObject({ code: 42902, limitScope: 'guest' });
    expect(mock.fetchMock).toHaveBeenCalledOnce();
  });
  it('aborts an unresponsive identity lookup so entry can offer guest access', async () => {
    vi.useFakeTimers();
    const mock = accountMock();
    mock.overrides['GET /auth/me'] = init => new Promise<Response>((_resolve, reject) => {
      init.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    });
    const assertion = expect(accountAPI.me()).rejects.toMatchObject({ code: 50301 });
    await vi.advanceTimersByTimeAsync(15_000); await assertion;
    expect(vi.getTimerCount()).toBe(0);
  });
});
