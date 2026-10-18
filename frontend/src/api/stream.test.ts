import { describe, expect, it, vi } from 'vitest';
import { sendChatStream, REQUEST_TIMEOUT_MS, STREAM_IDLE_TIMEOUT_MS } from './client';
import type { ChatRequest, ChatResponse, ChatStreamEvent } from './types';
import { controlledSSE, frame } from '../test/streams';

const request: ChatRequest = { session_id: 's', message: '有什么商品', debug: false };
const result: ChatResponse = { request_id: 'r', session_id: 's', answer: '你好[1]', route: 'rag',
  strategy: 'direct', sources: [{ index: 1, doc_id: 'd', title: '资料', snippet: '说明', score: .9 }], is_mock: false, trace: null };
const meta: ChatStreamEvent = { event: 'meta', data: { request_id: 'r', session_id: 's' } };

function jsonResponse() { return new Response(JSON.stringify(result), { headers: { 'Content-Type': 'application/json' } }); }

describe('POST streaming API', () => {
  it('defaults to SSE and decodes UTF-8 split at every byte', async () => {
    const input = [meta, { event: 'status', data: { stage: 'generating' } },
      { event: 'delta', data: { text: '你好[1]' } }, { event: 'done', data: result }] satisfies ChatStreamEvent[];
    const bytes = new TextEncoder().encode(': ping\r\n\r\n' + input.map(frame).join(''));
    const response = new Response(new ReadableStream<Uint8Array>({ start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    } }), { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } });
    vi.stubEnv('VITE_API_BASE', 'http://localhost:8010/');
    const fetchMock = vi.fn().mockResolvedValue(response);
    vi.stubGlobal('fetch', fetchMock);
    const received: ChatStreamEvent[] = [];
    expect(await sendChatStream(request, event => received.push(event))).toEqual(result);
    expect(received).toEqual(input);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledWith('http://localhost:8010/api/v1/chat/stream', expect.objectContaining({
      method: 'POST', body: JSON.stringify(request), headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    }));
  });

  it.each(['network', '404', '405', 'content-type', 'empty'])('falls back before any event: %s', async mode => {
    const fetchMock = vi.fn();
    if (mode === 'network') fetchMock.mockRejectedValueOnce(new TypeError('offline'));
    else if (mode === 'content-type') fetchMock.mockResolvedValueOnce(jsonResponse());
    else fetchMock.mockResolvedValueOnce(new Response('', { status: mode === 'empty' ? 200 : Number(mode),
      headers: { 'Content-Type': 'text/event-stream' } }));
    fetchMock.mockResolvedValueOnce(jsonResponse());
    vi.stubGlobal('fetch', fetchMock);
    const emit = vi.fn();
    expect(await sendChatStream(request, emit)).toEqual(result);
    expect(emit).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual(['/api/v1/chat/stream', '/api/v1/chat']);
  });

  it('VITE_STREAM=0 uses only the compatibility endpoint', async () => {
    vi.stubEnv('VITE_STREAM', '0');
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse());
    vi.stubGlobal('fetch', fetchMock);
    await sendChatStream(request, vi.fn());
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/chat');
  });

  it('preserves server error codes after an event and never falls back', async () => {
    const stream = controlledSSE();
    stream.event(meta);
    stream.event({ event: 'delta', data: { text: '部分' } });
    stream.event({ event: 'error', data: { code: 50201, message: '模型服务返回异常', request_id: 'r' } });
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    const emit = vi.fn();
    await expect(sendChatStream(request, emit)).rejects.toMatchObject({ code: 50201, requestId: 'r' });
    expect(emit).toHaveBeenCalledWith({ event: 'delta', data: { text: '部分' } });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each(['eof', 'invalid'])('does not fall back after meta on %s', async mode => {
    const stream = controlledSSE();
    stream.event(meta);
    if (mode === 'invalid') stream.text('event: delta\ndata: not-json\n\n');
    stream.close();
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    await expect(sendChatStream(request, vi.fn())).rejects.toMatchObject({ code: 50001 });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('enforces a 30-second event idle timeout and cancels the reader', async () => {
    vi.useFakeTimers();
    const stream = controlledSSE();
    stream.event(meta);
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    const emit = vi.fn();
    const assertion = expect(sendChatStream(request, emit)).rejects.toMatchObject({ code: 50401 });
    await vi.advanceTimersByTimeAsync(0);
    expect(emit).toHaveBeenCalledWith(meta);
    await vi.advanceTimersByTimeAsync(STREAM_IDLE_TIMEOUT_MS - 1);
    expect(stream.response.body?.locked).toBe(true);
    await vi.advanceTimersByTimeAsync(1);
    await assertion;
    expect(stream.response.body?.locked).toBe(false);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('retains the 60-second timeout until the first event, with no timeout fallback', async () => {
    vi.useFakeTimers();
    const stream = controlledSSE();
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    const assertion = expect(sendChatStream(request, vi.fn())).rejects.toMatchObject({ code: 50401 });
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
    await assertion;
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('cancels on caller abort without falling back or leaking a timer', async () => {
    vi.useFakeTimers();
    const stream = controlledSSE();
    stream.event(meta);
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();
    const assertion = expect(sendChatStream(request, vi.fn(), controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();
    await assertion;
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
    expect(stream.response.body?.locked).toBe(false);
  });

  it('streams Mock one character at a time with stages and no fetch', async () => {
    vi.useFakeTimers();
    vi.stubEnv('VITE_MOCK', '1');
    vi.spyOn(Math, 'random').mockReturnValue(0);
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const received: ChatStreamEvent[] = [];
    const pending = sendChatStream({ ...request, message: '你好' }, event => received.push(event));
    expect(received.map(event => event.event)).toEqual(['meta', 'status']);
    await vi.advanceTimersByTimeAsync(60);
    expect(received.some(event => event.event === 'delta')).toBe(true);
    expect(received.some(event => event.event === 'done')).toBe(false);
    await vi.runAllTimersAsync();
    const answer = await pending;
    expect(answer.is_mock).toBe(true);
    expect(received.filter(event => event.event === 'delta').map(event => event.data.text).join('')).toBe(answer.answer);
    expect(received.at(-1)).toEqual({ event: 'done', data: answer });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});


describe('stream audit boundaries', () => {
  it.each(['extension', 'malformed-known'])('keeps JSON fallback available before a valid event: %s', async kind => {
    const stream = controlledSSE();
    stream.text(kind === 'extension' ? 'event: extension\ndata: not-json\n\n' : 'event: delta\ndata: not-json\n\n');
    stream.close();
    const fetchMock = vi.fn().mockResolvedValueOnce(stream.response).mockResolvedValueOnce(jsonResponse());
    vi.stubGlobal('fetch', fetchMock);
    const emit = vi.fn();
    expect(await sendChatStream(request, emit)).toEqual(result);
    expect(emit).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual(['/api/v1/chat/stream', '/api/v1/chat']);
  });

  it('ignores extension events after meta without weakening the started-stream rule', async () => {
    const stream = controlledSSE();
    stream.event(meta);
    stream.text('event: extension\ndata: not-json\n\n');
    stream.close();
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    const emit = vi.fn();
    await expect(sendChatStream(request, emit)).rejects.toMatchObject({ code: 50001, requestId: 'r' });
    expect(emit.mock.calls).toEqual([[meta]]);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each([false, true])('accepts an error without request_id (meta=%s)', async withMeta => {
    const stream = controlledSSE();
    if (withMeta) stream.event(meta);
    stream.event({ event: 'error', data: { code: 42901, message: '提问有点快，休息一分钟再问吧' } });
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    await expect(sendChatStream(request, vi.fn())).rejects.toMatchObject({ code: 42901, requestId: withMeta ? 'r' : undefined });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each(['success', 'fallback', 'error'])('reader cleanup failures cannot replace %s', async outcome => {
    const wire = outcome === 'fallback' ? '' : frame(outcome === 'success'
      ? { event: 'done', data: result }
      : { event: 'error', data: { code: 50302, message: '服务有点忙，请稍后再试' } });
    const reader = {
      read: vi.fn().mockResolvedValueOnce(wire ? { done: false, value: new TextEncoder().encode(wire) } : { done: true }),
      cancel: vi.fn(() => { throw new TypeError('reader no longer owns the stream'); }),
      releaseLock: vi.fn(() => { throw new TypeError('already released'); }),
    };
    const response = { ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
      body: { locked: true, getReader: () => reader } } as unknown as Response;
    const fetchMock = vi.fn().mockResolvedValueOnce(response).mockResolvedValueOnce(jsonResponse());
    vi.stubGlobal('fetch', fetchMock);
    if (outcome === 'error') await expect(sendChatStream(request, vi.fn())).rejects.toMatchObject({ code: 50302 });
    else expect(await sendChatStream(request, vi.fn())).toEqual(result);
    expect(reader.cancel).toHaveBeenCalledOnce();
    expect(reader.releaseLock).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledTimes(outcome === 'fallback' ? 2 : 1);
  });

  it('does not release a reader whose stream is already unlocked', async () => {
    const reader = {
      read: vi.fn().mockResolvedValue({ done: false, value: new TextEncoder().encode(frame({ event: 'done', data: result })) }),
      cancel: vi.fn().mockRejectedValue(new TypeError('already released')),
      releaseLock: vi.fn(),
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true,
      headers: new Headers({ 'Content-Type': 'text/event-stream' }), body: { locked: false, getReader: () => reader } }));
    expect(await sendChatStream(request, vi.fn())).toEqual(result);
    expect(reader.releaseLock).not.toHaveBeenCalled();
  });
});
