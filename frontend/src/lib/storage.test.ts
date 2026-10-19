import { turn, conversation } from '../test/conversations';
import { describe, expect, it, vi } from 'vitest';
import { clearMessages, debugEnabled, getSessionId, loadMessages, MESSAGES_KEY, newSessionId, saveMessages, SESSION_KEY, TRACE_KEY, traceToggleVisible, loadTraceEnabled, saveTraceEnabled, CONVERSATIONS_KEY, MAX_STORAGE_LENGTH, loadConversations, saveConversations } from './storage';

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

describe('bounded and individually validated history snapshots', () => {
  it.each([false, true])('rejects raw input over 2MB before parsing, including when cleanup is blocked (%s)', blocked => {
    saveConversations([conversation('cached', 100)]);
    const oversized = '[]' + ' '.repeat(MAX_STORAGE_LENGTH - 1);
    localStorage.setItem(CONVERSATIONS_KEY, oversized);
    const parse = vi.spyOn(JSON, 'parse');
    const remove = vi.spyOn(Storage.prototype, 'removeItem');
    if (blocked) remove.mockImplementation(() => { throw new Error('blocked'); });
    expect(loadConversations()).toEqual([]);
    expect(parse).not.toHaveBeenCalled();
    expect(remove).toHaveBeenCalledWith(CONVERSATIONS_KEY);
    expect(localStorage.getItem(CONVERSATIONS_KEY)).toBe(blocked ? oversized : null);
    expect(loadConversations()).toEqual([]);
    expect(parse).not.toHaveBeenCalled();
  });
  it('accepts raw input at exactly the 2MB boundary', () => {
    const item = conversation('boundary', 100);
    const json = JSON.stringify([item]);
    const raw = json + ' '.repeat(MAX_STORAGE_LENGTH - json.length);
    localStorage.setItem(CONVERSATIONS_KEY, raw);
    const parse = vi.spyOn(JSON, 'parse');
    expect(loadConversations()).toEqual([item]);
    expect(parse).toHaveBeenCalledWith(raw);
    expect(localStorage.getItem(CONVERSATIONS_KEY)).toBe(raw);
  });
  it('limits raw parsed snapshots to the latest 30 sessions and 100 completed messages', () => {
    const items = Array.from({ length: 32 }, (_, index) => conversation(String(index), index));
    const latest = items[31];
    latest.messages = Array.from({ length: 51 }, (_, index) => turn(String(index))).flat();
    localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(items));
    const loaded = loadConversations();
    expect(loaded).toHaveLength(30);
    expect(loaded.map(item => item.id)).toEqual(items.slice(2).reverse().map(item => item.id));
    expect(loaded[0].messages).toEqual(latest.messages.slice(-100));
  });
  it.each(['read', 'write'])('truncates long text/answers/snippets/titles without dropping a completed turn (%s)', mode => {
    const item = conversation('long', 100);
    item.title = '题'.repeat(41);
    item.messages = turn('问'.repeat(4001), '答'.repeat(8001));
    const reply = item.messages[1];
    if (reply.kind !== 'assistant') throw new Error('expected fixture assistant');
    reply.response.sources = [{ index: 1, doc_id: 'doc', title: '源'.repeat(41), snippet: '摘'.repeat(501), score: 1 }];
    if (mode === 'read') localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify([item]));
    else saveConversations([item]);
    const loaded = loadConversations();
    expect(loaded).toHaveLength(1);
    expect(loaded[0].title).toBe('题'.repeat(40));
    expect(loaded[0].messages).toHaveLength(2);
    expect(loaded[0].messages[0]).toMatchObject({ kind: 'user', text: '问'.repeat(4000) });
    expect(loaded[0].messages[1]).toMatchObject({ kind: 'assistant', question: '问'.repeat(4000), response: {
      answer: '答'.repeat(8000), sources: [{ index: 1, doc_id: 'doc', title: '源'.repeat(40), snippet: '摘'.repeat(500), score: 1 }],
    } });
    if (mode === 'write') expect(JSON.parse(localStorage.getItem(CONVERSATIONS_KEY)!)).toEqual(loaded);
  });
  it('does not split surrogate pairs at a text limit', () => {
    saveMessages('emoji', turn('x'.repeat(3999) + '😀'), 100);
    expect(loadMessages('emoji')[0]).toMatchObject({ text: 'x'.repeat(3999) });
    expect(loadMessages('emoji')[1]).toMatchObject({ question: 'x'.repeat(3999) });
  });
  it('drops only malformed messages, repairing pairs across discarded entries', () => {
    const complete = [...turn('保留一'), ...turn('保留二')];
    const messages: unknown[] = [null, complete[0], { kind: 'assistant', id: 'broken', response: null }, complete[1],
      ...turn('丢弃未完成').slice(0, 1), complete[2], { kind: 'user', id: 'missing-text' }, complete[3]];
    localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify([{ ...conversation('mixed', 100), messages }]));
    expect(loadMessages('mixed')).toEqual(complete);
  });
  it('recovers a noncritical title but discards sessions with invalid IDs or timestamps', () => {
    localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify([
      { ...conversation('recover', 100), title: null }, { ...conversation('bad-id', 100), id: '' },
      { ...conversation('bad-time', 100), updatedAt: 'invalid' },
    ]));
    expect(loadConversations()).toEqual([{ ...conversation('recover', 100), title: 'recover' }]);
  });
  it.each([Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, Number.NaN])('rejects non-finite error codes on write (%s)', code => {
    const invalid = turn('错误', '', true);
    const error = invalid[1];
    if (error.kind !== 'error') throw new Error('expected fixture error');
    error.code = code;
    saveConversations([{ ...conversation('mixed', 100), messages: [...invalid, ...turn('保留')] }]);
    expect(loadMessages('mixed')).toEqual(turn('保留'));
  });
  it('rejects non-finite error codes from otherwise valid JSON on read', () => {
    const raw = JSON.stringify([{ ...conversation('mixed', 100), messages: [...turn('错误', '', true), ...turn('保留')] }])
      .replace('"code":50401', '"code":1e309');
    localStorage.setItem(CONVERSATIONS_KEY, raw);
    expect(loadMessages('mixed')).toEqual(turn('保留'));
  });
  it('normalizes in memory and skips disk when the bounded aggregate still exceeds 2MB', () => {
    saveConversations([conversation('on-disk', 100)]);
    const previous = localStorage.getItem(CONVERSATIONS_KEY);
    const items = Array.from({ length: 6 }, (_, index) => ({ ...conversation(String(index), index),
      messages: Array.from({ length: 50 }, (_, question) => turn(`${index}-${question}`, '答'.repeat(8001))).flat(),
    }));
    const write = vi.spyOn(Storage.prototype, 'setItem');
    const saved = saveConversations(items);
    expect(write).not.toHaveBeenCalled();
    expect(localStorage.getItem(CONVERSATIONS_KEY)).toBe(previous);
    expect(loadConversations()).toEqual(saved);
    expect(saved).toHaveLength(6);
    expect(saved[0].messages).toHaveLength(100);
    expect(saved[0].messages[1]).toMatchObject({ response: { answer: '答'.repeat(8000) } });
    saveConversations([saved[0]]);
    expect(write).toHaveBeenCalledTimes(1);
    expect(loadConversations()).toEqual([saved[0]]);
  });
  it('migrates legacy snapshots with per-message filtering and the same content limits', () => {
    sessionStorage.setItem(MESSAGES_KEY, JSON.stringify({ session_id: 'legacy', messages: [
      ...turn('问'.repeat(4001), '答'.repeat(8001)), null, ...turn('另一个'),
    ] }));
    const loaded = loadConversations(100);
    expect(loaded[0].messages).toHaveLength(4);
    expect(loaded[0].messages[0]).toMatchObject({ text: '问'.repeat(4000) });
    expect(loaded[0].messages[1]).toMatchObject({ question: '问'.repeat(4000), response: { answer: '答'.repeat(8000) } });
    expect(loaded[0].messages.slice(2)).toEqual(turn('另一个'));
    expect(sessionStorage.getItem(MESSAGES_KEY)).toBeNull();
  });
  it('never parses an oversized legacy snapshot', () => {
    sessionStorage.setItem(MESSAGES_KEY, '[]' + ' '.repeat(MAX_STORAGE_LENGTH));
    const parse = vi.spyOn(JSON, 'parse');
    expect(loadConversations()).toEqual([]);
    expect(parse).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(MESSAGES_KEY)).toBeNull();
  });
});
