import { useCallback, useEffect, useRef, useState } from 'react';
import { sendChatStream } from '../api/client';
import { ChatError } from '../api/errors';
import { accountError, conversationsAPI, type ConversationDetail } from '../api/account';
import type { ChatMessage, StreamProgress } from '../api/types';
import { characterCount } from '../lib/format';
import { clearMessages, completedMessages, createId, getSessionId, loadConversations, newSessionId, saveConversations, saveMessages, setSessionId as persistSessionId } from '../lib/storage';
import { buildHistory } from '../lib/history';

interface Options { remote?: boolean; onSettled?: () => void; onQuotaExceeded?: (scope?: 'guest' | 'user') => void }

export function useChat(traceEnabled: boolean, { remote = false, onSettled, onQuotaExceeded }: Options = {}) {
  const [initial] = useState(() => {
    const conversations = remote ? [] : loadConversations();
    const sessionId = remote ? createId() : getSessionId();
    return { sessionId, conversations, messages: conversations.find(item => item.id === sessionId)?.messages || [] };
  });
  const [conversations, setConversations] = useState(initial.conversations);
  const [sessionId, setSessionId] = useState(initial.sessionId);
  const [messages, setMessages] = useState<ChatMessage[]>(initial.messages);
  const [pending, setPending] = useState(false);
  const [progress, setProgress] = useState<StreamProgress | null>(null);
  const active = useRef<AbortController | null>(null);
  const epoch = useRef(0);
  const remoteId = useRef<string | undefined>();
  const [historyError, setHistoryError] = useState('');
  const [historyBusy, setHistoryBusy] = useState(false);
  const historyLoading = useRef(false);
  const listEpoch = useRef(0);
  const mounted = useRef(true);

  const refreshConversations = useCallback(async () => {
    if (!remote) return;
    const generation = ++listEpoch.current;
    try {
      const { items } = await conversationsAPI.list();
      if (!mounted.current || generation !== listEpoch.current) return;
      setConversations(items.map(item => ({ id: item.id, title: item.title,
        createdAt: Date.parse(item.updated_at), updatedAt: Date.parse(item.updated_at), messages: [] })));
      setHistoryError(previous => previous.startsWith('历史列表加载失败') ? '' : previous);
    } catch (error) {
      if (mounted.current && generation === listEpoch.current) setHistoryError(`历史列表加载失败。${accountError(error)}`);
    }
  }, [remote]);

  useEffect(() => { mounted.current = true; void refreshConversations(); return () => { mounted.current = false; listEpoch.current += 1; }; }, [refreshConversations]);

  useEffect(() => () => { epoch.current += 1; active.current?.abort(); active.current = null; }, []);

  const send = useCallback(async (input: string, retryId?: string): Promise<boolean> => {
    const question = input.trim();
    if (active.current || historyLoading.current || !question || characterCount(input) > 500) return false;
    const generation = ++epoch.current;
    const controller = new AbortController();
    const current = () => epoch.current === generation && !controller.signal.aborted;
    active.current = controller;
    setPending(true);
    setProgress({ text: '', stage: 'faq' });
    const userMessage: ChatMessage = { id: createId(), kind: 'user', text: question };
    if (!remote) setMessages(previous => !current() ? previous : retryId
      ? previous.filter(message => message.id !== retryId)
      : [...previous, userMessage]);
    const finish = (message: ChatMessage) => {
      if (!current()) return;
      const finished = retryId ? messages.map(item => item.id === retryId ? message : item) : [...messages, userMessage, message];
      setMessages(finished);
      // Failed first attempts remain visible but do not create a history entry.
      if (!remote && finished.some(item => item.kind === 'assistant')) setConversations(saveMessages(sessionId, finished));
    };
    const started = performance.now();
    let completedAt: number | undefined;
    let chatStarted = false;
    try {
      if (remote) setHistoryError('');
      if (remote && !remoteId.current) {
        const created = await conversationsAPI.create(controller.signal);
        if (!current()) return false;
        remoteId.current = created.id;
        setSessionId(created.id);
      }
      if (remote) setMessages(previous => !current() ? previous : retryId
        ? previous.filter(message => message.id !== retryId) : [...previous, userMessage]);
      chatStarted = true;
      const response = await sendChatStream({ ...(remote && remoteId.current ? { conversation_id: remoteId.current } : {}), session_id: remoteId.current || sessionId, message: question, debug: traceEnabled, history: buildHistory(messages) }, event => {
        if (!current() || active.current !== controller) return;
        if (event.event === 'done') completedAt = performance.now();
        if (event.event === 'status') setProgress(previous => !current() ? previous
          : { text: previous?.text || '', stage: event.data.stage });
        if (event.event === 'delta') setProgress(previous => !current() ? previous
          : { text: (previous?.text || '') + event.data.text, stage: previous?.stage || 'generating' });
      }, controller.signal);
      if (!current()) return false;
      const message: ChatMessage = { id: createId(), kind: 'assistant', response,
        elapsedMs: (completedAt ?? performance.now()) - started, question };
      finish(message);
    } catch (error) {
      if (!current()) return false;
      if (!chatStarted) { setHistoryError(`无法创建对话。${accountError(error)} 输入内容已保留，请重试。`); return false; }
      const failure = error instanceof ChatError ? error : new ChatError(50001);
      if (failure.code === 42902) onQuotaExceeded?.(failure.limitScope);
      const message: ChatMessage = { id: createId(), kind: 'error', code: failure.code,
        requestId: failure.requestId, limitScope: failure.limitScope, question };
      finish(message);
    } finally {
      if (mounted.current) { onSettled?.(); if (chatStarted) void refreshConversations(); }
      if (epoch.current === generation && active.current === controller) {
        active.current = null;
        setPending(false);
        setProgress(null);
      }
    }
    return true;
  }, [sessionId, traceEnabled, messages, remote, onSettled, onQuotaExceeded, refreshConversations]);

  const cancel = useCallback(() => {
    epoch.current += 1;
    active.current?.abort();
    active.current = null;
    setPending(false);
    setProgress(null);
    historyLoading.current = false;
    setHistoryBusy(false);
  }, []);

  const reset = useCallback(() => {
    cancel();
    setMessages([]);
    remoteId.current = undefined;
    setSessionId(remote ? createId() : newSessionId());
  }, [cancel, remote]);

  const selectConversation = useCallback((id: string): boolean | Promise<boolean> => {
    if (remote) {
      cancel();
      setMessages(previous => completedMessages(previous));
      historyLoading.current = true;
      setHistoryBusy(true);
      const generation = epoch.current;
      const controller = new AbortController();
      active.current = controller;
      return conversationsAPI.open(id, controller.signal).then(detail => {
        if (!mounted.current || epoch.current !== generation) return false;
        remoteId.current = id;
        setSessionId(id);
        setMessages(serverMessages(detail));
        setHistoryError('');
        return true;
      }).catch(error => {
        if (mounted.current && epoch.current === generation) setHistoryError(`无法打开对话。${accountError(error)}`);
        return false;
      }).finally(() => {
        if (mounted.current && epoch.current === generation) { active.current = null; historyLoading.current = false; setHistoryBusy(false); }
      });
    }
    const conversation = conversations.find(item => item.id === id);
    if (!conversation) return false;
    cancel();
    persistSessionId(id);
    setSessionId(id);
    setMessages(conversation.messages);
    return true;
  }, [cancel, conversations, remote]);

  const deleteConversation = useCallback((id: string) => {
    if (remote) {
      return conversationsAPI.remove(id).then(async () => {
        if (!mounted.current) return;
        setHistoryError('');
        const remaining = conversations.filter(item => item.id !== id);
        setConversations(remaining);
        if (id === remoteId.current) {
          reset();
          if (remaining[0]) await selectConversation(remaining[0].id);
        }
        await refreshConversations();
      }).catch(error => { if (mounted.current) setHistoryError(`删除失败。${accountError(error)}`); });
    }
    const remaining = saveConversations(loadConversations().filter(item => item.id !== id));
    setConversations(remaining);
    if (id === sessionId) {
      cancel();
      const next = remaining[0];
      if (next) { persistSessionId(next.id); setSessionId(next.id); setMessages(next.messages); }
      else reset();
    }
  }, [cancel, reset, sessionId, remote, conversations, selectConversation, refreshConversations]);

  const clearConversations = useCallback(() => {
    if (remote) return;
    clearMessages();
    setConversations([]);
    reset();
  }, [reset, remote]);

  const renameConversation = useCallback(async (id: string, title: string) => {
    if (!remote || !title.trim()) return false;
    try {
      await conversationsAPI.rename(id, title.trim());
      if (mounted.current) {
        setHistoryError('');
        setConversations(previous => previous.map(item => item.id === id ? { ...item, title: title.trim() } : item));
      }
      return true;
    } catch (error) { if (mounted.current) setHistoryError(`重命名失败。${accountError(error)}`); return false; }
  }, [remote]);

  return { sessionId, conversations, messages, pending: pending || historyBusy, historyError, refreshConversations, renameConversation, progress, send, reset, selectConversation, deleteConversation, clearConversations };
}

function serverMessages(detail: ConversationDetail): ChatMessage[] {
  let question = '';
  return detail.messages.map(message => {
    if (message.role === 'user') {
      question = message.content;
      return { id: createId(), kind: 'user', text: message.content };
    }
    return { id: createId(), kind: 'assistant', question, elapsedMs: 0, response: {
      request_id: '', session_id: detail.id, answer: message.content, route: message.route || 'general',
      sources: message.sources || [], strategy: null, trace: null, is_mock: false,
    } };
  });
}
