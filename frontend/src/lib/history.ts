import type { ChatMessage, HistoryMessage } from '../api/types';
import { completedMessages } from './storage';

const truncate = (text: string) => Array.from(text).slice(0, 500).join('');

/** Last three successful/refusal turns, chronological and strictly alternating. */
export function buildHistory(messages: ChatMessage[]): HistoryMessage[] {
  const complete = completedMessages(messages);
  const history: HistoryMessage[] = [];
  for (let i = 0; i < complete.length; i += 2) {
    const user = complete[i];
    const reply = complete[i + 1];
    if (user.kind !== 'user' || reply.kind !== 'assistant') continue;
    history.push({ role: 'user', content: truncate(user.text) },
      { role: 'assistant', content: truncate(reply.response.answer.replace(/\[\d+\]/g, '')) });
  }
  return history.slice(-6);
}
