import { describe, expect, it, vi } from 'vitest';
import { accountAPI, accountError, conversationsAPI } from './account';
import { ChatError } from './errors';
import { sendChat, sendChatStream } from './client';
import { accountMock, json, userIdentity } from '../test/account';
import { controlledSSE } from '../test/streams';
import { fixtureFor } from '../mocks/fixtures';

const request = { session_id: 's', message: '你好', debug: false, conversation_id: 'server-id' };

describe('shared account/history transport policy', () => {
  it.each([400, 401, 403, 404, 409, 422, 429])('does not retry HTTP %s, even without a business error envelope', async status => {
    vi.useFakeTimers();
    const mock = accountMock(userIdentity());
    mock.overrides['POST /conversations/c/messages'] = () => json({ detail: 'Rejected' }, status);
    await expect(conversationsAPI.saveAnswer('c', fixtureFor(request), 6000)).rejects.toMatchObject({ status });
    await vi.advanceTimersByTimeAsync(5000);
    expect(mock.calls('/conversations/c/messages', 'POST')).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });
  it.each([500, 502, 503, 504])('retries HTTP %s twice with exponential backoff and the identical payload', async status => {
    vi.useFakeTimers();
    const mock = accountMock(userIdentity());
    mock.overrides['POST /conversations/c/messages'] = () => json({ detail: 'Unavailable' }, status);
    const assertion = expect(conversationsAPI.saveAnswer('c', fixtureFor(request), 6000)).rejects.toMatchObject({ status });
    const calls = () => mock.calls('/conversations/c/messages', 'POST');
    await vi.advanceTimersByTimeAsync(0);
    expect(calls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(499);
    expect(calls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(calls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(999);
    expect(calls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    await assertion;
    expect(calls()).toHaveLength(3);
    expect(new Set(calls().map(([, init]) => init?.body)).size).toBe(1);
    expect(JSON.parse(String(calls()[0][1]?.body))).toMatchObject({ request_id: expect.any(String), elapsed_ms: 6000 });
    await vi.advanceTimersByTimeAsync(5000);
    expect(calls()).toHaveLength(3);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('reuses request_id after a committed write loses its response, without replaying chat or duplicating the message', async () => {
    vi.useFakeTimers();
    const mock = accountMock(userIdentity());
    // Model the backend's (conversation_id, request_id) upsert, including an ACK lost after commit.
    const stored = new Map<string, unknown>();
    let attempts = 0;
    mock.overrides['POST /conversations/c/messages'] = init => {
      const body = JSON.parse(String(init.body));
      stored.set(body.request_id, body);
      if (++attempts === 1) throw new TypeError('Network response lost after commit');
      return json({});
    };
    const response = fixtureFor(request);
    const saving = conversationsAPI.saveAnswer('c', response, 6000);
    await vi.advanceTimersByTimeAsync(500);
    await saving;
    await vi.advanceTimersByTimeAsync(5000);
    expect(mock.calls('/conversations/c/messages', 'POST')).toHaveLength(2);
    expect(stored.size).toBe(1);
    expect(stored.get(response.request_id)).toMatchObject({ content: response.answer, elapsed_ms: 6000 });
    expect(mock.calls('/chat')).toHaveLength(0);
    expect(mock.calls('/chat/stream')).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });
  it('adds X-Requested-With only to account/history writes and includes Cookie policy everywhere', async () => {
    const mock = accountMock(userIdentity());
    await accountAPI.me();
    await accountAPI.authenticate('register', 'demo_123', 'demo_pass_123');
    await accountAPI.authenticate('login', 'demo_123', 'demo_pass_123');
    await conversationsAPI.list();
    const created = await conversationsAPI.create();
    await conversationsAPI.open(created.id);
    await conversationsAPI.rename(created.id, '新标题');
    await conversationsAPI.saveAnswer(created.id, fixtureFor(request), 6000);
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
  it.each([6000, 3_600_001])('bounds answer persistence and omits unsupported elapsed values (%s)', async elapsedMs => {
    const mock = accountMock(userIdentity());
    const created = await conversationsAPI.create();
    const response = { ...fixtureFor(request), answer: '😀'.repeat(4001), sources: Array.from({ length: 9 }, () => ({
      index: 1, doc_id: '文'.repeat(129), title: '题'.repeat(201), snippet: '摘'.repeat(501), score: 1,
    })) };
    await conversationsAPI.saveAnswer(created.id, response, elapsedMs);
    const body = JSON.parse(String(mock.calls(`/conversations/${created.id}/messages`, 'POST')[0][1]?.body));
    expect(body.request_id).toBe(response.request_id);
    expect(Array.from(body.content)).toHaveLength(4000);
    expect(body.sources).toHaveLength(8);
    expect(body.sources[0]).toEqual({ index: 1, doc_id: '文'.repeat(128), title: '题'.repeat(200), snippet: '摘'.repeat(500), score: 1 });
    if (elapsedMs === 6000) expect(body.elapsed_ms).toBe(6000);
    else expect(body).not.toHaveProperty('elapsed_ms');
  });
  it('joins the configured API base for auth and history without changing Cookie policy', async () => {
    vi.stubEnv('VITE_API_BASE', 'https://api.example.test/');
    const mock = accountMock(userIdentity()); await accountAPI.me(); await conversationsAPI.list();
    expect(mock.fetchMock.mock.calls.map(([url]) => url)).toEqual([
      'https://api.example.test/api/v1/auth/me', 'https://api.example.test/api/v1/conversations',
    ]);
  });
  it.each([40001, 40002, 40003, 40101, 40102, 40103, 40301, 40401, 40901, 42903, 50301])('preserves envelope fields for business error %s', async code => {
    const mock = accountMock();
    mock.overrides['POST /auth/login'] = () => json({ code, message: '可展示的中文原因', retry_after_seconds: 7 }, 400);
    await expect(accountAPI.authenticate('login', 'demo_123', 'demo_pass_123')).rejects.toMatchObject({ code, reason: '可展示的中文原因', retryAfterSeconds: 7 });
  });
  it.each([
    [40002, '用户名须为 3–20 位字母、数字或下划线。'],
    [40003, '密码须为 8–64 位，不能与用户名相同、为纯数字或常见弱口令。'],
    [40102, '账号不存在。'], [40103, '密码错误。'], [40901, '用户名已被使用。'],
    [42903, '请求过多或账号暂时锁定，请稍后再试。'],
    [40301, '访问来源未被允许，请使用正确的访问地址。'],
  ])('maps code %s to a Chinese account error without a server message', (code, message) => {
    expect(accountError(new ChatError(Number(code)))).toBe(message);
  });
  it.each(['json', 'http-sse', 'event-sse'])('explains forbidden origin through %s', async transport => {
    const mock = accountMock();
    const body = { code: 40301, message: '请求来源校验失败' };
    mock.overrides['POST /chat'] = () => json(body, 403);
    mock.overrides['POST /chat/stream'] = () => json(body, 403);
    if (transport === 'event-sse') {
      const stream = controlledSSE(); stream.event({ event: 'error', data: body });
      mock.overrides['POST /chat/stream'] = () => stream.response;
    }
    await expect(transport === 'json' ? sendChat(request) : sendChatStream(request, vi.fn())).rejects.toMatchObject({
      code: 40301, message: '访问来源未被允许，请使用正确的访问地址。',
    });
    expect(mock.fetchMock).toHaveBeenCalledOnce();
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
