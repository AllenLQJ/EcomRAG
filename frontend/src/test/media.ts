import { vi } from 'vitest';

/** jsdom has no layout engine; drive the same matchMedia change events as a browser. */
export function mockMobile(initial = true): (mobile: boolean) => void {
  let mobile = initial;
  const listeners = new Set<() => void>();
  vi.stubGlobal('matchMedia', vi.fn((media: string) => ({
    media,
    get matches() { return mobile; },
    addEventListener: (_type: string, listener: () => void) => { listeners.add(listener); },
    removeEventListener: (_type: string, listener: () => void) => { listeners.delete(listener); },
  })));
  return next => { mobile = next; listeners.forEach(listener => listener()); };
}
