import { useCallback, useEffect, useRef, useState } from 'react';
import { sendChat } from '../api/client';
import { ChatError } from '../api/errors';
import type { ChatMessage } from '../api/types';
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
  const active = useRef<AbortController | null>(null);

  useEffect(() => { saveMessages(sessionId, messages); }, [sessionId, messages]);
  useEffect(() => () => { active.current?.abort(); active.current = null; }, []);

  const send = useCallback(async (input: string, retryId?: string): Promise<boolean> => {
    const question = input.trim();
    if (active.current || !question || characterCount(input) > 500) return false;
    const controller = new AbortController();
    active.current = controller;
    setPending(true);
    setMessages(previous => retryId
      ? previous.filter(message => message.id !== retryId)
      : [...previous, { id: createId(), kind: 'user', text: question }]);
    const started = performance.now();
    try {
      const response = await sendChat({ session_id: sessionId, message: question, debug: traceEnabled }, controller.signal);
      if (controller.signal.aborted) return false;
      setMessages(previous => [...previous, {
        id: createId(), kind: 'assistant', response, elapsedMs: performance.now() - started, question,
      }]);
    } catch (error) {
      if (controller.signal.aborted) return false;
      const failure = error instanceof ChatError ? error : new ChatError(50001);
      setMessages(previous => [...previous, {
        id: createId(), kind: 'error', code: failure.code, requestId: failure.requestId, question,
      }]);
    } finally {
      if (active.current === controller) { active.current = null; setPending(false); }
    }
    return true;
  }, [sessionId, traceEnabled]);

  const reset = useCallback(() => {
    active.current?.abort();
    active.current = null;
    setPending(false);
    clearMessages();
    setMessages([]);
    setSessionId(newSessionId());
  }, []);

  return { messages, pending, send, reset };
}
