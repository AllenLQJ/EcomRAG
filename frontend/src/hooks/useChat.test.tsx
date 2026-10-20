import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import * as api from '../api/client';
import { conversationsAPI } from '../api/account';
import { ChatError } from '../api/errors';
import type { ChatRequest, ChatResponse, ChatStreamEvent } from '../api/types';
import { CONVERSATIONS_KEY, MESSAGES_KEY, SESSION_KEY, saveConversations, loadConversations } from '../lib/storage';
import { conversation, turn } from '../test/conversations';
import { product, productAnswer } from '../test/products';
import { useChat } from './useChat';

interface Attempt {
  request: ChatRequest;
  signal?: AbortSignal;
  emit: (event: ChatStreamEvent) => void;
  resolve: (response: ChatResponse) => void;
  reject: (error: Error) => void;
}

function fakeTransport() {
  const attempts: Attempt[] = [];
  vi.spyOn(api, 'sendChatStream').mockImplementation((request, emit, signal) => new Promise<ChatResponse>((resolve, reject) => {
    attempts.push({ request, emit, resolve, reject, signal });
  }));
  return attempts;
}

function answer(attempt: Attempt, text: string): ChatResponse {
  return { request_id: 'r', session_id: attempt.request.session_id, answer: text,
    route: 'general', strategy: null, sources: [], is_mock: false, trace: null };
}

function pendingTimingWrite() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((done, fail) => { resolve = done; reject = fail; });
  const list = vi.spyOn(conversationsAPI, 'list').mockResolvedValue({ items: [] });
  vi.spyOn(conversationsAPI, 'create').mockResolvedValue({ id: 'server-1', title: '', updated_at: '2026-10-20T00:00:00Z', message_count: 0 });
  const save = vi.spyOn(conversationsAPI, 'saveAnswer').mockResolvedValue(undefined).mockReturnValueOnce(promise);
  return { resolve, reject, list, save };
}

describe('remote timing writes and live reply ownership', () => {
  it.each(['resolve', 'reject'] as const)('does not block regeneration or restore cards when an older timing write finishes (%s)', async outcome => {
    const write = pendingTimingWrite();
    const attempts = fakeTransport();
    const onSettled = vi.fn();
    let clock = 1000;
    vi.spyOn(performance, 'now').mockImplementation(() => clock);
    const { result } = renderHook(() => useChat(false, { remote: true, onSettled }));
    let first!: Promise<boolean>;
    await act(async () => { first = result.current.send('台灯规格'); });
    const response = productAnswer([product()]);
    await act(async () => {
      clock = 7000;
      attempts[0].emit({ event: 'done', data: response });
      attempts[0].resolve(response);
    });
    // The timing write is still unresolved. The completed chat must already be usable.
    expect(result.current.pending).toBe(false);
    await expect(first).resolves.toBe(true);
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(write.save).toHaveBeenCalledWith('server-1', response, 6000);
    const oldReply = result.current.messages[1];
    expect(oldReply).toMatchObject({ kind: 'assistant', elapsedMs: 6000, response: { products: [product()] } });

    let regeneration!: Promise<boolean>;
    await act(async () => { regeneration = result.current.send('台灯规格', oldReply.id); });
    expect(result.current.messages.map(message => message.kind)).toEqual(['user']);
    expect(attempts[1].request.history).toEqual([]);
    act(() => { attempts[1].emit({ event: 'delta', data: { text: '新的部分回答' } }); });
    const listsBeforeCompletion = write.list.mock.calls.length;
    await act(async () => {
      if (outcome === 'resolve') write.resolve();
      else write.reject(new ChatError(50301));
    });
    expect(result.current.pending).toBe(true);
    expect(result.current.progress?.text).toBe('新的部分回答');
    expect(result.current.messages.map(message => message.kind)).toEqual(['user']);
    expect(result.current.historyError).toBe('');
    expect(write.list).toHaveBeenCalledTimes(listsBeforeCompletion);
    expect(onSettled).toHaveBeenCalledTimes(1);

    await act(async () => {
      clock = 13000;
      const replacement = productAnswer();
      attempts[1].emit({ event: 'done', data: replacement });
      attempts[1].resolve(replacement);
      await regeneration;
    });
    expect(result.current.messages[1]).toMatchObject({ kind: 'assistant', elapsedMs: 6000 });
    expect(result.current.messages[1].kind === 'assistant' && result.current.messages[1].response.products).toBeUndefined();
    expect(write.save).toHaveBeenCalledTimes(2);
    expect(result.current.pending).toBe(false);
  });

  it.each(['reset', 'open', 'unmount'] as const)('ignores a late timing failure after %s', async action => {
    const write = pendingTimingWrite();
    const attempts = fakeTransport();
    vi.spyOn(conversationsAPI, 'open').mockResolvedValue({ id: 'saved', title: '历史', messages: [
      { role: 'user', content: '旧问题', created_at: '' },
      { role: 'assistant', content: '历史回答', route: 'faq', sources: [], elapsed_ms: 2000, created_at: '' },
    ] });
    const { result, unmount } = renderHook(() => useChat(false, { remote: true }));
    await act(async () => { void result.current.send('台灯规格'); });
    await act(async () => { attempts[0].resolve(productAnswer([product()])); });
    expect(result.current.pending).toBe(false);
    if (action === 'reset') act(() => result.current.reset());
    else if (action === 'open') await act(async () => { await result.current.selectConversation('saved'); });
    else unmount();
    const messages = result.current.messages;
    const listCalls = write.list.mock.calls.length;
    await act(async () => { write.reject(new ChatError(50301)); });
    expect(result.current.messages).toBe(messages);
    expect(result.current.historyError).toBe('');
    expect(write.list).toHaveBeenCalledTimes(listCalls);
    if (action === 'reset') expect(result.current.messages).toEqual([]);
    if (action === 'open') {
      expect(result.current.messages[1]).toMatchObject({ kind: 'assistant', elapsedMs: 2000, response: { answer: '历史回答' } });
      expect(result.current.messages[1].kind === 'assistant' && result.current.messages[1].response.products).toBeUndefined();
    }
  });

  it.each([404, 503, 401])('keeps the answer and timing on HTTP %s, and only requests login on 401', async status => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const onAuthRequired = vi.fn();
    const write = pendingTimingWrite();
    const attempts = fakeTransport();
    const { result } = renderHook(() => useChat(false, { remote: true, onAuthRequired }));
    await act(async () => { void result.current.send('台灯规格'); });
    await act(async () => { attempts[0].resolve(productAnswer([product()])); });
    expect(result.current.pending).toBe(false);
    const messages = result.current.messages;
    await act(async () => { write.reject(new ChatError(status * 100 + 1, undefined, undefined, status)); });
    expect(warning).toHaveBeenCalledOnce();
    expect(result.current.historyError).toBe('');
    expect(onAuthRequired).toHaveBeenCalledTimes(status === 401 ? 1 : 0);
    expect(result.current.messages).toBe(messages);
    expect(result.current.messages.map(message => message.kind)).toEqual(['user', 'assistant']);
    expect(result.current.pending).toBe(false);
    expect(result.current.messages[1]).toMatchObject({ kind: 'assistant', elapsedMs: expect.any(Number) });
    await act(async () => { void result.current.send('继续'); });
    expect(attempts).toHaveLength(2);
    await act(async () => { attempts[1].resolve(answer(attempts[1], '继续回答')); });
    expect(result.current.messages.map(message => message.kind)).toEqual(['user', 'assistant', 'user', 'assistant']);
  });
});

describe('request epochs and completed conversation storage', () => {
  it('ignores old deltas and done after a new conversation even when transport ignores abort', async () => {
    vi.spyOn(AbortController.prototype, 'abort').mockImplementation(() => undefined);
    const attempts = fakeTransport();
    const { result } = renderHook(() => useChat(false));
    let old!: Promise<boolean>;
    act(() => { old = result.current.send('旧问题'); });
    const oldSession = attempts[0].request.session_id;
    act(() => { result.current.reset(); });
    let fresh!: Promise<boolean>;
    act(() => { fresh = result.current.send('新问题'); });
    expect(attempts[1].request.session_id).not.toBe(oldSession);
    await act(async () => {
      attempts[0].emit({ event: 'delta', data: { text: '旧文本' } });
      const response = answer(attempts[0], '旧答案');
      attempts[0].emit({ event: 'done', data: response });
      attempts[0].resolve(response);
      await old;
    });
    expect(result.current.pending).toBe(true);
    expect(result.current.progress?.text).toBe('');
    expect(result.current.messages.map(message => message.kind)).toEqual(['user']);
    expect(JSON.stringify(result.current.messages)).not.toMatch(/旧问题|旧答案|旧文本/);
    await act(async () => {
      attempts[1].emit({ event: 'delta', data: { text: '新文本' } });
    });
    expect(result.current.progress?.text).toBe('新文本');
    await act(async () => {
      const response = answer(attempts[1], '新答案');
      attempts[1].emit({ event: 'done', data: response });
      attempts[1].resolve(response);
      await fresh;
    });
    expect(result.current.pending).toBe(false);
    expect(JSON.stringify(result.current.messages)).toContain('新答案');
    expect(JSON.stringify(result.current.messages)).not.toContain('旧答案');
  });

  it('retry advances the epoch and ignores late events from the failed attempt', async () => {
    const attempts = fakeTransport();
    const { result } = renderHook(() => useChat(false));
    let first!: Promise<boolean>;
    act(() => { first = result.current.send('同一个问题'); });
    await act(async () => {
      attempts[0].reject(new ChatError(50201, 'req-123'));
      await first;
    });
    const error = result.current.messages.find(message => message.kind === 'error');
    expect(error).toMatchObject({ requestId: 'req-123' });
    expect(result.current.conversations).toEqual([]);
    let retry!: Promise<boolean>;
    act(() => { retry = result.current.send('同一个问题', error!.id); });
    act(() => {
      attempts[0].emit({ event: 'delta', data: { text: '已失败请求的迟到增量' } });
      attempts[0].emit({ event: 'done', data: answer(attempts[0], '迟到结果') });
    });
    expect(result.current.progress?.text).toBe('');
    expect(result.current.pending).toBe(true);
    expect(attempts[1].request).toEqual(attempts[0].request);
    await act(async () => {
      attempts[1].resolve(answer(attempts[1], '重试成功'));
      await retry;
    });
    expect(result.current.messages.map(message => message.kind)).toEqual(['user', 'assistant']);
    expect(JSON.stringify(result.current.messages)).toContain('重试成功');
    expect(JSON.stringify(result.current.messages)).not.toContain('迟到');
  });

  it('does not persist an in-flight user message, then saves the completed pair', async () => {
    const attempts = fakeTransport();
    const { result } = renderHook(() => useChat(false));
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.send('正在发送的问题'); });
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.conversations).toEqual([]);
    expect(localStorage.getItem(CONVERSATIONS_KEY)).toBeNull();
    await act(async () => {
      attempts[0].resolve(answer(attempts[0], '完整回答'));
      await pending;
    });
    expect(JSON.parse(localStorage.getItem(CONVERSATIONS_KEY)!)[0].messages).toHaveLength(2);
  });

  it('discards a trailing user from legacy storage and restores without a loading state', () => {
    localStorage.setItem(SESSION_KEY, 's');
    sessionStorage.setItem(MESSAGES_KEY, JSON.stringify({ session_id: 's', messages: [
      { id: 'u', kind: 'user', text: '未完成的问题' },
    ] }));
    const { result } = renderHook(() => useChat(false));
    expect(result.current.messages).toEqual([]);
    expect(result.current.pending).toBe(false);
    expect(result.current.progress).toBeNull();
  });
});

describe('multiple conversation state', () => {
  it('switches with abort/epoch protection even when the old transport resolves late', async () => {
    const old = conversation('old', 100);
    const next = conversation('next', 200);
    saveConversations([old, next]);
    localStorage.setItem(SESSION_KEY, 'old');
    const attempts = fakeTransport();
    const { result } = renderHook(() => useChat(false));
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.send('未完成'); });
    act(() => result.current.selectConversation('next'));
    expect(attempts[0].signal?.aborted).toBe(true);
    expect(result.current.messages).toEqual(next.messages);
    await act(async () => {
      attempts[0].emit({ event: 'delta', data: { text: '迟到文本' } });
      attempts[0].resolve(answer(attempts[0], '迟到答案'));
      await pending;
    });
    expect(result.current.sessionId).toBe('next');
    expect(result.current.pending).toBe(false);
    expect(result.current.progress).toBeNull();
    expect(result.current.messages).toEqual(next.messages);
    expect(loadConversations().find(item => item.id === 'old')?.messages).toEqual(old.messages);
  });
  it('sends history from the selected conversation and starts new sessions with empty history', async () => {
    const selected = { ...conversation('selected'), messages: [...turn('问题', '回答[1]'), ...turn('失败', '', true)] };
    saveConversations([selected]);
    const attempts = fakeTransport();
    const { result } = renderHook(() => useChat(false));
    act(() => result.current.selectConversation('selected'));
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.send('继续'); });
    expect(attempts[0].request).toMatchObject({ session_id: 'selected', history: [
      { role: 'user', content: '问题' }, { role: 'assistant', content: '回答' },
    ] });
    await act(async () => { attempts[0].resolve(answer(attempts[0], '完整回答')); await pending; });
    expect(loadConversations()[0].messages).toHaveLength(6);
    act(() => result.current.reset());
    expect(result.current.conversations).toHaveLength(1);
    act(() => { pending = result.current.send('新问题'); });
    expect(attempts[1].request.history).toEqual([]);
    expect(attempts[1].request.session_id).not.toBe('selected');
    expect(result.current.conversations).toHaveLength(1);
    await act(async () => { attempts[1].resolve(answer(attempts[1], '新回答')); await pending; });
    expect(result.current.conversations).toHaveLength(2);
  });
  it('does not list a failed first attempt but retains its error for retry', async () => {
    const attempts = fakeTransport();
    const { result } = renderHook(() => useChat(false));
    let pending!: Promise<boolean>;
    act(() => { pending = result.current.send('失败'); });
    await act(async () => { attempts[0].reject(new ChatError(50401, 'req')); await pending; });
    expect(result.current.messages[1]).toMatchObject({ kind: 'error', requestId: 'req' });
    expect(result.current.conversations).toEqual([]);
    expect(loadConversations()).toEqual([]);
  });
});

describe('deletion against the latest stored history', () => {
  it('preserves externally added/updated conversations and does not resurrect externally removed ones', () => {
    saveConversations([conversation('delete', 100), conversation('removed', 200), conversation('updated', 300)]);
    localStorage.setItem(SESSION_KEY, 'delete');
    const { result } = renderHook(() => useChat(false));
    const fresh = { ...conversation('updated', 400), messages: turn('外部更新') };
    const external = conversation('external', 500);
    // Simulate another tab writing directly, while this hook still has its original React snapshot.
    localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify([conversation('delete', 100), fresh, external]));
    act(() => result.current.deleteConversation('delete'));
    expect(loadConversations()).toEqual([external, fresh]);
    expect(result.current.conversations).toEqual([external, fresh]);
    expect(result.current.sessionId).toBe('external');
    expect(result.current.messages).toEqual(external.messages);
  });
  it('uses the latest storage even when deleting a noncurrent session', () => {
    saveConversations([conversation('current', 100), conversation('delete', 200)]);
    localStorage.setItem(SESSION_KEY, 'current');
    const { result } = renderHook(() => useChat(false));
    const external = conversation('external', 300);
    localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify([conversation('current', 100), conversation('delete', 200), external]));
    act(() => result.current.deleteConversation('delete'));
    expect(result.current.sessionId).toBe('current');
    expect(loadConversations().map(item => item.id)).toEqual(['external', 'current']);
  });
});
