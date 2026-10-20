import { ChatError } from './errors';
import { isRecord } from './validate';

/** All transports share origin, Cookie and CSRF-header policy. */
export function apiFetch(path: string, init: RequestInit = {}, chat = false): Promise<Response> {
  const base = (import.meta.env.VITE_API_BASE || '').replace(/\/+$/, '');
  const headers = init.headers instanceof Headers ? Object.fromEntries(init.headers.entries())
    : Array.isArray(init.headers) ? Object.fromEntries(init.headers) : { ...init.headers };
  if (!chat && ['POST', 'PATCH', 'DELETE'].includes(init.method || 'GET')) headers['X-Requested-With'] = 'ecomrag';
  return fetch(`${base}/api/v1${path}`, { ...init, headers, credentials: 'same-origin' });
}

export function responseError(body: unknown): ChatError {
  return new ChatError(isRecord(body) && typeof body.code === 'number' ? body.code : 50001,
    isRecord(body) && typeof body.request_id === 'string' ? body.request_id : undefined,
    isRecord(body) ? body : undefined);
}

export async function apiJSON<T>(path: string, method = 'GET', data?: unknown, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  const timer = window.setTimeout(cancel, 15_000);
  try {
    const response = await apiFetch(path, { method, signal: controller.signal, headers: { Accept: 'application/json',
      ...(data === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) throw responseError(body);
    return body as T;
  } catch (error) {
    if (signal?.aborted) throw error;
    throw error instanceof ChatError ? error : new ChatError(50301);
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener('abort', cancel);
  }
}
