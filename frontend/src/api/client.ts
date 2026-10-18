import { ChatError } from './errors';
import type { ChatRequest, ChatResponse } from './types';
import { isChatResponse, isRecord } from './validate';
import { mockChat } from '../mocks/fixtures';
import { characterCount } from '../lib/format';

export const REQUEST_TIMEOUT_MS = 60_000;

/** The UI depends only on this request/response boundary, ready for a future SSE adapter. */
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
    const base = (import.meta.env.VITE_API_BASE || '').replace(/\/+$/, '');
    const response = await fetch(`${base}/api/v1/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(request),
      signal: controller.signal,
    });
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ChatError(
        isRecord(body) && typeof body.code === 'number' ? body.code : 50001,
        isRecord(body) && typeof body.request_id === 'string' ? body.request_id : undefined,
      );
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
