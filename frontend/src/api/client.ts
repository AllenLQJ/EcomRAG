import { ChatError } from './errors';
import { apiFetch, responseError } from './http';
import type { ChatRequest, ChatResponse, ChatStreamEvent, StreamStage } from './types';
import { SSEParser, type SSEFrame } from './sse';
import { isChatResponse, isRecord } from './validate';
import { mockChat, mockChatStream } from '../mocks/fixtures';
import { characterCount } from '../lib/format';

export const REQUEST_TIMEOUT_MS = 60_000;

/** Non-streaming compatibility endpoint. */
export async function sendChat(request: ChatRequest, signal?: AbortSignal): Promise<ChatResponse> {
  if (!request.session_id.trim() || characterCount(request.session_id) > 128
    || !request.message.trim() || characterCount(request.message) > 500) {
    throw new ChatError(40001);
  }
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
  try {
    if (import.meta.env.VITE_MOCK === '1') return await mockChat(request, controller.signal);
    const response = await apiFetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(request),
      signal: controller.signal,
    }, true);
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw responseError(body);
    }
    if (!isChatResponse(body)) throw new ChatError(50001);
    return body;
  } catch (error) {
    if (timedOut) throw new ChatError(50401);
    if (signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
    throw error instanceof ChatError ? error : new ChatError(50001);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', cancel);
  }
}

export const STREAM_IDLE_TIMEOUT_MS = 30_000;

/** POST SSE, falling back only when no SSE event has been received. */
export async function sendChatStream(
  request: ChatRequest, onEvent: (event: ChatStreamEvent) => void, signal?: AbortSignal,
): Promise<ChatResponse> {
  if (import.meta.env.VITE_STREAM === '0') return sendChat(request, signal);
  if (!request.session_id.trim() || characterCount(request.session_id) > 128
    || !request.message.trim() || characterCount(request.message) > 500) throw new ChatError(40001);
  const controller = new AbortController();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let stream: ReadableStream<Uint8Array> | undefined;
  const cancelReader = () => {
    try { void reader?.cancel().catch(() => undefined); } catch { /* Reader may already be released. */ }
  };
  const cancel = () => { controller.abort(); cancelReader(); };
  if (signal?.aborted) controller.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  let timedOut = false;
  let received = false;
  let result: ChatResponse | undefined;
  let requestId: string | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const arm = (ms: number) => {
    clearTimeout(timer);
    timer = setTimeout(() => { timedOut = true; controller.abort(); cancelReader(); }, ms);
  };
  arm(REQUEST_TIMEOUT_MS);
  let fallback = false;
  try {
    if (import.meta.env.VITE_MOCK === '1') {
      return await mockChatStream(request, event => {
        received = true;
        arm(STREAM_IDLE_TIMEOUT_MS);
        onEvent(event);
      }, controller.signal);
    }
    const response = await apiFetch('/chat/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(request), signal: controller.signal,
    }, true);
    const contentType = response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase();
    if (!response.ok) {
      const failure = responseError(await response.json().catch(() => null));
      // Business rejections must not repeat a question through the JSON fallback.
      if ([40001, 40101, 40301, 42901, 42902, 50301].includes(failure.code)) { received = true; throw failure; }
      throw failure;
    }
    if (contentType !== 'text/event-stream' || !response.body) {
      await response.body?.cancel();
      throw new ChatError(50001);
    }
    stream = response.body;
    reader = stream.getReader();
    const parser = new SSEParser(frame => {
      if (result) return;
      const event = parseStreamEvent(frame);
      if (!event) return; // Unknown events neither start the stream nor reset its idle timer.
      received = true;
      arm(STREAM_IDLE_TIMEOUT_MS);
      if (event.event === 'meta') requestId = event.data.request_id;
      if (event.event === 'error') {
        onEvent(event);
        throw new ChatError(event.data.code, event.data.request_id ?? requestId, { ...event.data });
      }
      if (event.event === 'done') result = event.data;
      onEvent(event);
    });
    const decoder = new TextDecoder();
    while (!result) {
      const chunk = await reader.read();
      if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
      if (chunk.done) {
        parser.push(decoder.decode());
        parser.finish();
        break;
      }
      parser.push(decoder.decode(chunk.value, { stream: true }));
    }
    if (!result) throw new ChatError(50001, requestId);
    return result;
  } catch (error) {
    if (signal?.aborted) throw new DOMException('Request cancelled', 'AbortError');
    if (timedOut) throw new ChatError(50401, requestId);
    if (received) throw error instanceof ChatError ? error : new ChatError(50001, requestId);
    fallback = true;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', cancel);
    controller.abort();
    const ownedReader = reader;
    reader = undefined;
    if (ownedReader) {
      try { await ownedReader.cancel(); } catch { /* Cleanup must not replace success/error/fallback. */ }
      try { if (stream?.locked) ownedReader.releaseLock(); } catch { /* Already released or invalidated. */ }
    }
  }
  if (fallback) return sendChat(request, signal);
  throw new ChatError(50001);
}

function parseStreamEvent(frame: SSEFrame): ChatStreamEvent | undefined {
  if (!['meta', 'status', 'delta', 'done', 'error'].includes(frame.event)) return undefined;
  const data: unknown = JSON.parse(frame.data);
  if (!isRecord(data)) throw new ChatError(50001);
  if (frame.event === 'meta' && typeof data.request_id === 'string' && typeof data.session_id === 'string') {
    return { event: 'meta', data: { request_id: data.request_id, session_id: data.session_id } };
  }
  if (frame.event === 'status' && ['faq', 'routing', 'retrieving', 'generating'].includes(String(data.stage))) {
    return { event: 'status', data: { stage: data.stage as StreamStage } };
  }
  if (frame.event === 'delta' && typeof data.text === 'string') return { event: 'delta', data: { text: data.text } };
  if (frame.event === 'done' && isChatResponse(data)) return { event: 'done', data };
  if (frame.event === 'error' && typeof data.code === 'number' && typeof data.message === 'string'
    && (data.request_id === undefined || typeof data.request_id === 'string')) {
    return { event: 'error', data: { code: data.code, message: data.message,
      ...(data.limit_scope === 'guest' || data.limit_scope === 'user' ? { limit_scope: data.limit_scope } : {}),
      ...(typeof data.request_id === 'string' ? { request_id: data.request_id } : {}) } };
  }
  throw new ChatError(50001);
}
