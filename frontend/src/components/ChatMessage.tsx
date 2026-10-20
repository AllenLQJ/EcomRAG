import { useState } from 'react';
import type { ChatMessage as Message, StreamStage } from '../api/types';
import { errorMessage } from '../api/errors';
import { formatSeconds } from '../lib/format';
import { AnswerMarkdown } from './AnswerMarkdown';
import { AssistantAvatar } from './Brand';
import { RouteBadge } from './RouteBadge';
import { ProductCards } from './ProductCards';
import { Sources } from './Sources';
import { Trace } from './Trace';

interface Props {
  message: Message;
  pending: boolean;
  onLogin?: () => void;
  quotaRole?: 'guest' | 'user';
  showTrace: boolean;
  supportUrl?: string;
  onRetry: (question: string, id: string) => void;
  onRephrase: (question: string) => void;
}

export function ChatMessage({ message, pending, showTrace, supportUrl, onRetry, onRephrase, onLogin, quotaRole }: Props) {
  const [selection, setSelection] = useState<{ index: number; sequence: number } | null>(null);
  if (message.kind === 'user') return <div className="flex justify-end">
    <p className="max-w-[85%] whitespace-pre-wrap break-words rounded-user bg-ink px-4 py-3 text-base leading-relaxed text-card sm:max-w-bubble sm:px-[18px]">{message.text}</p>
  </div>;

  const fallback = message.kind === 'assistant' && message.response.route === 'fallback';
  return <div className="flex gap-3 sm:gap-4">
    <div className={fallback ? 'hidden sm:block' : ''}><AssistantAvatar /></div>
    <div className="min-w-0 flex-1 space-y-3">
      {message.kind === 'error' ? <div className="rounded-card border border-line bg-card p-4">
        <p className="text-sm leading-relaxed text-fallback-text">{message.code === 42902 && (message.limitScope || quotaRole) === 'guest' ? '访客今日次数已用完，登录后每天可提问 50 次。' : errorMessage(message.code)}</p>
        {message.code === 42902 && (message.limitScope || quotaRole) === 'guest'
          ? <button type="button" onClick={onLogin} className="secondary-button mt-3">登录/注册</button>
          : message.code !== 42902 && <button type="button" disabled={pending} onClick={() => onRetry(message.question, message.id)} className="secondary-button mt-3 disabled:opacity-50">重试</button>}
      </div> : <>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <RouteBadge route={message.response.route} /><span>{formatSeconds(message.elapsedMs)}</span>
        </div>
        <AnswerMarkdown answer={message.response.answer} sources={message.response.sources}
          onCitation={index => setSelection(previous => ({ index, sequence: (previous?.sequence || 0) + 1 }))} />
        <ProductCards products={message.response.products} />
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

const stageText: Record<StreamStage, string> = {
  faq: '正在查找常见问题…', routing: '正在理解问题…',
  retrieving: '正在查阅店铺资料…', generating: '正在生成回答…',
};

export function StreamingMessage({ text }: { text: string }) {
  return <div className="flex gap-3 sm:gap-4"><AssistantAvatar />
    <div className="min-w-0 flex-1"><AnswerMarkdown answer={text} sources={[]} onCitation={() => undefined} /></div>
  </div>;
}

export function LoadingMessage({ stage = 'retrieving' }: { stage?: StreamStage }) {
  return <div className="flex items-start gap-3 sm:gap-4"><AssistantAvatar />
    <div aria-live="off" className="flex min-h-8 flex-wrap items-center gap-3 text-sm text-muted">
      <span aria-hidden="true" className="inline-flex gap-1">
        {[0, 1, 2].map(index => <span key={index} className="h-1.5 w-1.5 animate-thinking rounded-full bg-ink motion-reduce:animate-none" style={{ animationDelay: `${index * 0.2}s` }} />)}
      </span>{stageText[stage]}
    </div>
  </div>;
}
