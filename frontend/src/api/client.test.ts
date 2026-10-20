import { afterEach, describe, expect, it, vi } from 'vitest';
import { REQUEST_TIMEOUT_MS, sendChat } from './client';
import { errorMessage } from './errors';
import type { ChatRequest, ChatResponse } from './types';

const request: ChatRequest = { session_id: 'test-session', message: '怎么申请退货？', debug: false, history: [{ role: 'user', content: '你好' }, { role: 'assistant', content: '你好，请问有什么需要？' }] };
const success: ChatResponse = {
  request_id: 'r-1', session_id: request.session_id, answer: '可以申请。',
  route: 'faq', strategy: null, sources: [], is_mock: false, trace: null,
};

function response(body: unknown, ok = true) {
  return { ok, json: async () => body } as Response;
}

afterEach(() => { vi.useRealTimers(); });

describe('API client', () => {
  it('posts the exact contract and joins API base without duplicate slashes', async () => {
    vi.stubEnv('VITE_API_BASE', 'http://localhost:8010/');
    const fetchMock = vi.fn().mockResolvedValue(response(success));
    vi.stubGlobal('fetch', fetchMock);
    expect(await sendChat(request)).toEqual(success);
    expect(fetchMock).toHaveBeenCalledWith('http://localhost:8010/api/v1/chat', expect.objectContaining({
      method: 'POST', body: JSON.stringify(request), signal: expect.any(AbortSignal),
    }));
  });

  it.each([
    [40301, '访问来源未被允许，请使用正确的访问地址。'],
    [42901, '提问有点快，休息一分钟再问吧。'],
    [42902, '今天提问次数已达上限，请明天再试。'],
    [50401, '回答超时了，请重试一次。'],
    [40001, '输入有点问题，请检查后重试。'],
    [40401, '服务有点忙，请稍后再试。'],
    [40501, '服务有点忙，请稍后再试。'],
    [50001, '服务有点忙，请稍后再试。'],
    [50201, '服务有点忙，请稍后再试。'],
    [50301, '服务有点忙，请稍后再试。'],
    [59999, '服务有点忙，请稍后再试。'],
  ] as const)('maps error %i and preserves request_id', async (code, message) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ code, message: 'internal text', request_id: 'request-42' }, false)));
    await expect(sendChat(request)).rejects.toMatchObject({ code, message, requestId: 'request-42' });
    expect(errorMessage(code)).toBe(message);
  });

  it('maps network failures to the busy message', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(sendChat(request)).rejects.toMatchObject({ code: 50001, message: '服务有点忙，请稍后再试。' });
  });

  it.each([true, false])('handles non-JSON responses (ok=%s)', async ok => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok, json: async () => { throw new SyntaxError('HTML response'); } }));
    await expect(sendChat(request)).rejects.toMatchObject({ code: 50001 });
  });

  it('rejects malformed success payloads', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ answer: 'incomplete' })));
    await expect(sendChat(request)).rejects.toMatchObject({ code: 50001 });
  });

  it('aborts at 60 seconds and maps the timeout to 50401', async () => {
    expect(REQUEST_TIMEOUT_MS).toBe(60_000);
    expect(REQUEST_TIMEOUT_MS).toBeGreaterThan(40_000); // 后端默认整个请求预算为 40 秒。
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      const requestSignal = init.signal as AbortSignal;
      signals.push(requestSignal);
      requestSignal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    })));
    const assertion = expect(sendChat(request)).rejects.toMatchObject({ code: 50401, message: '回答超时了，请重试一次。' });
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
    await assertion;
    expect(signals[0].aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('distinguishes a caller cancellation from a timeout', async () => {
    const controller = new AbortController();
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal!.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    })));
    const assertion = expect(sendChat(request, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
    controller.abort();
    await assertion;
  });

  it('validates blank/long messages and session IDs without fetching', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    for (const invalid of [
      { ...request, message: '  ' }, { ...request, message: '字'.repeat(501) },
      { ...request, session_id: 'x'.repeat(129) }, { ...request, session_id: '' },
    ]) await expect(sendChat(invalid)).rejects.toMatchObject({ code: 40001 });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
