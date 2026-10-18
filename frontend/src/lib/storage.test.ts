import { describe, expect, it, vi } from 'vitest';
import { clearMessages, debugEnabled, getSessionId, loadMessages, MESSAGES_KEY, newSessionId, saveMessages, SESSION_KEY } from './storage';

describe('storage', () => {
  it('creates/reuses a UUID and replaces it for a new conversation', () => {
    const first = getSessionId();
    expect(first).toMatch(/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i);
    expect(getSessionId()).toBe(first);
    expect(localStorage.getItem(SESSION_KEY)).toBe(first);
    expect(newSessionId()).not.toBe(first);
  });

  it('persists messages for the matching session and clears them', () => {
    const messages = [{ id: 'm1', kind: 'user' as const, text: '你好' }];
    saveMessages('s1', messages);
    expect(loadMessages('s1')).toEqual(messages);
    expect(loadMessages('s2')).toEqual([]);
    clearMessages();
    expect(loadMessages('s1')).toEqual([]);
  });

  it('ignores invalid JSON or malformed messages', () => {
    sessionStorage.setItem(MESSAGES_KEY, '{');
    expect(loadMessages('s')).toEqual([]);
    sessionStorage.setItem(MESSAGES_KEY, JSON.stringify({ session_id: 's', messages: [{ kind: 'assistant', id: '1', response: null }] }));
    expect(loadMessages('s')).toEqual([]);
  });

  it('survives throwing storage methods', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('blocked'); });
    expect(getSessionId()).toBeTruthy();
    expect(newSessionId()).toBeTruthy();
    expect(loadMessages('s')).toEqual([]);
    expect(() => saveMessages('s', [])).not.toThrow();
    expect(() => clearMessages()).not.toThrow();
    expect(debugEnabled()).toBe(false);
  });

  it('survives throwing storage property accessors', () => {
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('SecurityError'); });
    vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => { throw new Error('SecurityError'); });
    expect(getSessionId()).toBeTruthy();
    expect(loadMessages('s')).toEqual([]);
    expect(() => saveMessages('s', [])).not.toThrow();
    expect(() => clearMessages()).not.toThrow();
  });

  it('enables debug only with the explicit URL or localStorage flag', () => {
    expect(debugEnabled()).toBe(false);
    localStorage.setItem('ecomrag.debug', '1');
    expect(debugEnabled()).toBe(true);
    localStorage.removeItem('ecomrag.debug');
    window.history.replaceState({}, '', '/?debug=1');
    expect(debugEnabled()).toBe(true);
  });
});
