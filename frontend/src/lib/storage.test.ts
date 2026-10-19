import { turn, conversation } from '../test/conversations';
import { describe, expect, it, vi } from 'vitest';
import { clearMessages, debugEnabled, getSessionId, loadMessages, MESSAGES_KEY, newSessionId, saveMessages, SESSION_KEY, TRACE_KEY, traceToggleVisible, loadTraceEnabled, saveTraceEnabled, CONVERSATIONS_KEY, loadConversations, saveConversations } from './storage';

describe('storage', () => {
  it('creates/reuses a UUID and replaces it for a new conversation', () => {
    const first = getSessionId();
    expect(first).toMatch(/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i);
    expect(getSessionId()).toBe(first);
    expect(localStorage.getItem(SESSION_KEY)).toBe(first);
    expect(newSessionId()).not.toBe(first);
  });

  it('persists messages for the matching session and clears them', () => {
    const messages = [{ id: 'm1', kind: 'user' as const, text: '你好' },
      { id: 'm2', kind: 'error' as const, code: 50401, question: '你好' }];
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
    expect(traceToggleVisible()).toBe(false);
    expect(loadTraceEnabled()).toBe(false);
    expect(() => saveTraceEnabled(true)).not.toThrow();
  });

  it('survives throwing storage property accessors', () => {
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('SecurityError'); });
    vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => { throw new Error('SecurityError'); });
    expect(getSessionId()).toBeTruthy();
    expect(loadMessages('s')).toEqual([]);
    expect(() => saveMessages('s', [])).not.toThrow();
    expect(() => clearMessages()).not.toThrow();
    expect(traceToggleVisible()).toBe(false);
    expect(loadTraceEnabled()).toBe(false);
    expect(() => saveTraceEnabled(true)).not.toThrow();
  });

  it('enables debug only with the explicit URL or localStorage flag', () => {
    expect(debugEnabled()).toBe(false);
    localStorage.setItem('ecomrag.debug', '1');
    expect(debugEnabled()).toBe(true);
    localStorage.removeItem('ecomrag.debug');
    window.history.replaceState({}, '', '/?debug=1');
    expect(debugEnabled()).toBe(true);
  });
  it('persists only the explicit trace switch in sessionStorage', () => {
    expect(loadTraceEnabled()).toBe(false);
    saveTraceEnabled(true);
    expect(sessionStorage.getItem(TRACE_KEY)).toBe('1');
    expect(loadTraceEnabled()).toBe(true);
    saveTraceEnabled(false);
    expect(loadTraceEnabled()).toBe(false);
    expect(localStorage.getItem(TRACE_KEY)).toBeNull();
  });

  it.each(['true', 'yes', '0', ''])('ignores non-1 trace visibility flags (%s)', flag => {
    vi.stubEnv('VITE_SHOW_TRACE_TOGGLE', flag);
    expect(traceToggleVisible()).toBe(false);
  });

  it('uses the build flag even when localStorage is inaccessible', () => {
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('SecurityError'); });
    vi.stubEnv('VITE_SHOW_TRACE_TOGGLE', '1');
    expect(traceToggleVisible()).toBe(true);
  });

});


describe('completed-turn snapshots', () => {
  it('never saves a trailing unfinished user message', () => {
    saveMessages('s', [{ id: 'u', kind: 'user', text: '等待中的提问' }]);
    expect(loadConversations()).toEqual([]);
    expect(sessionStorage.getItem(MESSAGES_KEY)).toBeNull();
    expect(loadMessages('s')).toEqual([]);
  });

  it('drops orphan user tails from legacy storage but keeps completed error turns', () => {
    const complete = [{ id: 'u1', kind: 'user' as const, text: '已结束的提问' },
      { id: 'e1', kind: 'error' as const, code: 50401, question: '已结束的提问' }];
    const messages = [...complete, { id: 'u2', kind: 'user' as const, text: '孤立提问' }];
    sessionStorage.setItem(MESSAGES_KEY, JSON.stringify({ session_id: 's', messages }));
    expect(loadMessages('s')).toEqual(complete);
    saveMessages('s', messages);
    expect(loadConversations()[0].messages).toEqual(complete);
    expect(sessionStorage.getItem(MESSAGES_KEY)).toBeNull();
  });
});

describe('versioned conversation history', () => {
  it('round-trips multiple conversations and updates timestamps without changing the first title', () => {
    saveMessages('one', turn('😀'.repeat(21)), 100);
    saveMessages('two', turn('第二个'), 200);
    saveMessages('one', [...turn('😀'.repeat(21)), ...turn('继续')], 300);
    const stored = JSON.parse(localStorage.getItem(CONVERSATIONS_KEY)!);
    expect(stored.map((item: { id: string }) => item.id)).toEqual(['one', 'two']);
    expect(stored[0]).toMatchObject({ id: 'one', title: '😀'.repeat(20), createdAt: 100, updatedAt: 300 });
    expect(loadConversations()).toEqual(stored);
    expect(sessionStorage.getItem(MESSAGES_KEY)).toBeNull();
  });
  it('keeps the latest 30 conversations and latest 100 messages in complete pairs', () => {
    saveConversations(Array.from({ length: 31 }, (_, index) => conversation(String(index), index)));
    expect(loadConversations()).toHaveLength(30);
    expect(loadConversations().some(item => item.id === '0')).toBe(false);
    const messages = Array.from({ length: 51 }, (_, index) => turn(String(index))).flat();
    saveMessages('30', [...messages, { id: 'pending', kind: 'user', text: '未完成' }], 100);
    expect(loadMessages('30')).toEqual(messages.slice(2));
    expect(loadMessages('30')).toHaveLength(100);
  });
  it('migrates completed legacy turns exactly once and deletes the legacy snapshot', () => {
    localStorage.setItem(SESSION_KEY, 'legacy');
    sessionStorage.setItem(MESSAGES_KEY, JSON.stringify({ session_id: 'legacy', messages: [
      ...turn('旧问题'), ...turn('失败', '', true), { id: 'pending', kind: 'user', text: '未完成' },
    ] }));
    expect(loadConversations(100)[0]).toMatchObject({ id: 'legacy', title: '旧问题', createdAt: 100, updatedAt: 100 });
    expect(loadMessages('legacy')).toEqual([...turn('旧问题'), ...turn('失败', '', true)]);
    expect(sessionStorage.getItem(MESSAGES_KEY)).toBeNull();
    expect(loadConversations(200)).toHaveLength(1);
    expect(loadConversations()[0].updatedAt).toBe(100);
  });
  it('rejects malformed records and tolerates malformed JSON', () => {
    localStorage.setItem(CONVERSATIONS_KEY, '{');
    expect(loadConversations()).toEqual([]);
    localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify([null, { ...conversation('bad'), messages: [{}] }, conversation('good')]));
    expect(loadConversations().map(item => item.id)).toEqual(['good']);
  });
  it.each(['quota', 'methods', 'accessors'])('keeps history operations in memory when storage fails: %s', mode => {
    saveMessages('original', turn('已有'), 100);
    if (mode === 'accessors') {
      vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('blocked'); });
      vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => { throw new Error('blocked'); });
    } else {
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
      if (mode === 'methods') vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    }
    saveMessages('new', turn('新问题'), 200);
    expect(loadMessages('new')).toEqual(turn('新问题'));
    expect(loadConversations()).toHaveLength(2);
    saveConversations(loadConversations().filter(item => item.id !== 'original'));
    expect(loadConversations().map(item => item.id)).toEqual(['new']);
    clearMessages();
    expect(loadConversations()).toEqual([]);
  });
});

describe('legacy selection and volatile session IDs', () => {
  it('selects a migrated legacy session when the current-id key is absent', () => {
    sessionStorage.setItem(MESSAGES_KEY, JSON.stringify({ session_id: 'legacy', messages: turn('迁移') }));
    loadConversations(100);
    expect(getSessionId()).toBe('legacy');
    expect(sessionStorage.getItem(MESSAGES_KEY)).toBeNull();
  });
  it('retains the newest current session in memory after a failed write', () => {
    localStorage.setItem(SESSION_KEY, 'stale');
    expect(getSessionId()).toBe('stale');
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
    const fresh = newSessionId();
    expect(fresh).not.toBe('stale');
    expect(getSessionId()).toBe(fresh);
  });
});

describe('legacy migration cleanup failures', () => {
  it('does not resurrect deleted legacy history when removeItem is blocked', () => {
    sessionStorage.setItem(MESSAGES_KEY, JSON.stringify({ session_id: 'legacy', messages: turn('迁移') }));
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('blocked'); });
    expect(loadConversations(100)).toHaveLength(1);
    clearMessages();
    expect(loadConversations()).toEqual([]);
  });
});
