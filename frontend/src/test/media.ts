import { vi } from 'vitest';

/** jsdom has no layout engine; drive the same matchMedia change events as a browser. */
export function mockMobile(initial = true): (mobile: boolean) => void {
  const resize = mockViewportWidth(initial ? 390 : 1280);
  return mobile => resize(mobile ? 390 : 1280);
}

/** Evaluate both CSS min-width and JS max-width queries against the same viewport. */
export function mockViewportWidth(initial: number): (width: number) => void {
  let width = initial;
  const listeners = new Set<() => void>();
  vi.stubGlobal('matchMedia', vi.fn((media: string) => ({
    media,
    get matches() {
      const query = media.match(/^\((min|max)-width: (\d+)px\)$/);
      return Boolean(query && (query[1] === 'min' ? width >= Number(query[2]) : width <= Number(query[2])));
    },
    addEventListener: (_type: string, listener: () => void) => { listeners.add(listener); },
    removeEventListener: (_type: string, listener: () => void) => { listeners.delete(listener); },
  })));
  return next => { width = next; listeners.forEach(listener => listener()); };
}
