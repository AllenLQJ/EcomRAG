import { useCallback, useEffect, useRef, useState } from 'react';
import { sendChatStream } from '../api/client';
import { ChatError } from '../api/errors';
import type { ChatMessage, StreamProgress } from '../api/types';
import { characterCount } from '../lib/format';
import { clearMessages, createId, getSessionId, loadConversations, newSessionId, saveConversations, saveMessages, setSessionId as persistSessionId } from '../lib/storage';
import { buildHistory } from '../lib/history';

export function useChat(traceEnabled: boolean) {
  const [initial] = useState(() => {
    const conversations = loadConversations();
    const sessionId = getSessionId();
    return { sessionId, conversations, messages: conversations.find(item => item.id === sessionId)?.messages || [] };
  });
  const [conversations, setConversations] = useState(initial.conversations);
  const [sessionId, setSessionId] = useState(initial.sessionId);
  const [messages, setMessages] = useState<ChatMessage[]>(initial.messages);
  const [pending, setPending] = useState(false);
  const [progress, setProgress] = useState<StreamProgress | null>(null);
  const active = useRef<AbortController | null>(null);
  const epoch = useRef(0);

  useEffect(() => () => { epoch.current += 1; active.current?.abort(); active.current = null; }, []);

  const send = useCallback(async (input: string, retryId?: string): Promise<boolean> => {
    const question = input.trim();
    if (active.current || !question || characterCount(input) > 500) return false;
    const generation = ++epoch.current;
    const controller = new AbortController();
    const current = () => epoch.current === generation && !controller.signal.aborted;
    active.current = controller;
    setPending(true);
    setProgress({ text: '', stage: 'faq' });
    const userMessage: ChatMessage = { id: createId(), kind: 'user', text: question };
    setMessages(previous => !current() ? previous : retryId
      ? previous.filter(message => message.id !== retryId)
      : [...previous, userMessage]);
    const finish = (message: ChatMessage) => {
      if (!current()) return;
      const finished = retryId ? messages.map(item => item.id === retryId ? message : item) : [...messages, userMessage, message];
      setMessages(finished);
      // Failed first attempts remain visible but do not create a history entry.
      if (finished.some(item => item.kind === 'assistant')) setConversations(saveMessages(sessionId, finished));
    };
    const started = performance.now();
    let completedAt: number | undefined;
    try {
      const response = await sendChatStream({ session_id: sessionId, message: question, debug: traceEnabled, history: buildHistory(messages) }, event => {
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
      const failure = error instanceof ChatError ? error : new ChatError(50001);
      const message: ChatMessage = { id: createId(), kind: 'error', code: failure.code,
        requestId: failure.requestId, question };
      finish(message);
    } finally {
      if (epoch.current === generation && active.current === controller) {
        active.current = null;
        setPending(false);
        setProgress(null);
      }
    }
    return true;
  }, [sessionId, traceEnabled, messages]);

  const cancel = useCallback(() => {
    epoch.current += 1;
    active.current?.abort();
    active.current = null;
    setPending(false);
    setProgress(null);
  }, []);

  const reset = useCallback(() => {
    cancel();
    setMessages([]);
    setSessionId(newSessionId());
  }, [cancel]);

  const selectConversation = useCallback((id: string) => {
    const conversation = conversations.find(item => item.id === id);
    if (!conversation) return;
    cancel();
    persistSessionId(id);
    setSessionId(id);
    setMessages(conversation.messages);
  }, [cancel, conversations]);

  const deleteConversation = useCallback((id: string) => {
    const remaining = saveConversations(loadConversations().filter(item => item.id !== id));
    setConversations(remaining);
    if (id === sessionId) {
      cancel();
      const next = remaining[0];
      if (next) { persistSessionId(next.id); setSessionId(next.id); setMessages(next.messages); }
      else reset();
    }
  }, [cancel, reset, sessionId]);

  const clearConversations = useCallback(() => {
    clearMessages();
    setConversations([]);
    reset();
  }, [reset]);

  return { sessionId, conversations, messages, pending, progress, send, reset, selectConversation, deleteConversation, clearConversations };
}
