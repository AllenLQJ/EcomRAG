import { useCallback, useEffect, useRef, useState } from 'react';
import { sendChatStream } from '../api/client';
import { ChatError } from '../api/errors';
import type { ChatMessage, StreamProgress } from '../api/types';
import { characterCount } from '../lib/format';
import { clearMessages, createId, getSessionId, loadMessages, newSessionId, saveMessages } from '../lib/storage';

export function useChat(traceEnabled: boolean) {
  const [initial] = useState(() => {
    const sessionId = getSessionId();
    return { sessionId, messages: loadMessages(sessionId) };
  });
  const [sessionId, setSessionId] = useState(initial.sessionId);
  const [messages, setMessages] = useState<ChatMessage[]>(initial.messages);
  const [pending, setPending] = useState(false);
  const [progress, setProgress] = useState<StreamProgress | null>(null);
  const active = useRef<AbortController | null>(null);
  const epoch = useRef(0);

  useEffect(() => { saveMessages(sessionId, messages); }, [sessionId, messages]);
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
    const started = performance.now();
    let completedAt: number | undefined;
    try {
      const response = await sendChatStream({ session_id: sessionId, message: question, debug: traceEnabled }, event => {
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
      setMessages(previous => current() ? [...previous, message] : previous);
    } catch (error) {
      if (!current()) return false;
      const failure = error instanceof ChatError ? error : new ChatError(50001);
      const message: ChatMessage = { id: createId(), kind: 'error', code: failure.code,
        requestId: failure.requestId, question };
      setMessages(previous => current() ? [...previous, message] : previous);
    } finally {
      if (epoch.current === generation && active.current === controller) {
        active.current = null;
        setPending(false);
        setProgress(null);
      }
    }
    return true;
  }, [sessionId, traceEnabled]);

  const reset = useCallback(() => {
    epoch.current += 1;
    active.current?.abort();
    active.current = null;
    setPending(false);
    setProgress(null);
    clearMessages();
    setMessages([]);
    setSessionId(newSessionId());
  }, []);

  return { messages, pending, progress, send, reset };
}
