import { useState } from 'react';
import type { ChatMessage as Message } from '../api/types';
import { errorMessage } from '../api/errors';
import { formatSeconds } from '../lib/format';
import { AnswerMarkdown } from './AnswerMarkdown';
import { AssistantAvatar } from './Brand';
import { RouteBadge } from './RouteBadge';
import { Sources } from './Sources';
import { Trace } from './Trace';

interface Props {
  message: Message;
  pending: boolean;
  showTrace: boolean;
  supportUrl?: string;
  onRetry: (question: string, id: string) => void;
  onRephrase: (question: string) => void;
}

export function ChatMessage({ message, pending, showTrace, supportUrl, onRetry, onRephrase }: Props) {
  const [selection, setSelection] = useState<{ index: number; sequence: number } | null>(null);
  if (message.kind === 'user') return <div className="flex justify-end">
    <p className="max-w-[85%] whitespace-pre-wrap break-words rounded-user bg-ink px-4 py-3 text-base leading-relaxed text-card sm:max-w-bubble sm:px-[18px]">{message.text}</p>
  </div>;

  const fallback = message.kind === 'assistant' && message.response.route === 'fallback';
  return <div className="flex gap-3 sm:gap-4">
    <div className={fallback ? 'hidden sm:block' : ''}><AssistantAvatar /></div>
    <div className="min-w-0 flex-1 space-y-3">
      {message.kind === 'error' ? <div className="rounded-card border border-line bg-card p-4">
        <p className="text-sm leading-relaxed text-fallback-text">{errorMessage(message.code)}</p>
        <button type="button" disabled={pending} onClick={() => onRetry(message.question, message.id)} className="secondary-button mt-3 disabled:opacity-50">重试</button>
      </div> : <>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <RouteBadge route={message.response.route} /><span>{formatSeconds(message.elapsedMs)}</span>
          {message.response.is_mock && <span className="font-mono text-xs">Mock · 样例</span>}
        </div>
        <AnswerMarkdown answer={message.response.answer} sources={message.response.sources}
          onCitation={index => setSelection(previous => ({ index, sequence: (previous?.sequence || 0) + 1 }))} />
        {fallback && <div className="flex flex-wrap gap-2">
          {supportUrl && <a href={supportUrl} target="_blank" rel="noopener noreferrer" className="primary-button flex-1 sm:flex-none">联系人工客服</a>}
          <button type="button" disabled={pending} onClick={() => onRephrase(message.question)} className="secondary-button flex-1 disabled:opacity-50 sm:flex-none">换个问法</button>
        </div>}
        <Sources sources={message.response.sources} selection={selection} />
        {showTrace && <Trace trace={message.response.trace} />}
      </>}
    </div>
  </div>;
}

export function LoadingMessage() {
  return <div className="flex items-start gap-3 sm:gap-4"><AssistantAvatar />
    <div role="status" className="flex min-h-8 flex-wrap items-center gap-3 text-sm text-muted">
      <span aria-hidden="true" className="inline-flex gap-1">
        {[0, 1, 2].map(index => <span key={index} className="h-1.5 w-1.5 animate-thinking rounded-full bg-ink motion-reduce:animate-none" style={{ animationDelay: `${index * 0.2}s` }} />)}
      </span>正在查阅店铺资料…
    </div>
  </div>;
}
