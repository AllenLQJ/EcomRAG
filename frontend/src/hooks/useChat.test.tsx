import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import * as api from '../api/client';
import { ChatError } from '../api/errors';
import type { ChatRequest, ChatResponse, ChatStreamEvent } from '../api/types';
import { MESSAGES_KEY, SESSION_KEY } from '../lib/storage';
import { useChat } from './useChat';

interface Attempt {
  request: ChatRequest;
  emit: (event: ChatStreamEvent) => void;
  resolve: (response: ChatResponse) => void;
  reject: (error: Error) => void;
}

function fakeTransport() {
  const attempts: Attempt[] = [];
  vi.spyOn(api, 'sendChatStream').mockImplementation((request, emit) => new Promise<ChatResponse>((resolve, reject) => {
    attempts.push({ request, emit, resolve, reject });
  }));
  return attempts;
}

function answer(attempt: Attempt, text: string): ChatResponse {
  return { request_id: 'r', session_id: attempt.request.session_id, answer: text,
    route: 'general', strategy: null, sources: [], is_mock: false, trace: null };
}

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
      attempts[0].reject(new ChatError(50201));
      await first;
    });
    const error = result.current.messages.find(message => message.kind === 'error');
    expect(error).toBeDefined();
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
    expect(JSON.parse(sessionStorage.getItem(MESSAGES_KEY)!).messages).toEqual([]);
    await act(async () => {
      attempts[0].resolve(answer(attempts[0], '完整回答'));
      await pending;
    });
    expect(JSON.parse(sessionStorage.getItem(MESSAGES_KEY)!).messages).toHaveLength(2);
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
