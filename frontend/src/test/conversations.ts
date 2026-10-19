import type { ChatMessage } from '../api/types';
import type { Conversation } from '../lib/storage';

export function turn(question: string, answer = `回答：${question}`, error = false): ChatMessage[] {
  return [{ id: `u-${question}`, kind: 'user', text: question }, error
    ? { id: `a-${question}`, kind: 'error', question, code: 50401, requestId: 'req-123' }
    : { id: `a-${question}`, kind: 'assistant', question, elapsedMs: 100, response: {
      request_id: 'r', session_id: 's', answer, route: 'general', strategy: null, sources: [], is_mock: false, trace: null,
    } }];
}

export function conversation(id: string, updatedAt = Date.now()): Conversation {
  return { id, title: id, createdAt: updatedAt, updatedAt, messages: turn(id) };
}
