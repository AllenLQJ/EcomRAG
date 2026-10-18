import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';
import { TextDecoder, TextEncoder } from 'node:util';
import { ReadableStream } from 'node:stream/web';

beforeEach(() => {
  if (typeof globalThis.TextEncoder === 'undefined') vi.stubGlobal('TextEncoder', TextEncoder);
  if (typeof globalThis.TextDecoder === 'undefined') vi.stubGlobal('TextDecoder', TextDecoder);
  if (typeof globalThis.ReadableStream === 'undefined') vi.stubGlobal('ReadableStream', ReadableStream);
  localStorage.clear();
  sessionStorage.clear();
  window.history.replaceState({}, '', '/');
  vi.stubEnv('VITE_API_BASE', '');
  vi.stubEnv('VITE_MOCK', '0');
  vi.stubEnv('VITE_STREAM', '1');
  vi.stubEnv('VITE_SHOW_TRACE_TOGGLE', '0');
  vi.stubEnv('VITE_STORE_NAME', '灵码');
  vi.stubEnv('VITE_SHOW_SAMPLE_BADGE', 'true');
  vi.stubEnv('VITE_HUMAN_SUPPORT_URL', '');
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true, value: vi.fn(), writable: true,
  });
});

afterEach(() => { cleanup(); vi.useRealTimers(); });
