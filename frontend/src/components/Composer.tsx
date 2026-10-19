import { forwardRef, useId, useLayoutEffect, useRef, useState } from 'react';
import { characterCount } from '../lib/format';
import { Chevron, SendIcon } from './Brand';
import { useMobile } from '../hooks/useMobile';

export const QUICK_QUESTIONS = ['运费怎么算', '发票怎么开', '签收后发现屏幕有划痕可以退吗？', '怎么申请退货？'];

interface Props {
  value: string;
  onChange: (value: string) => void;
  onSend: (value: string) => void;
  pending: boolean;
}

export const Composer = forwardRef<HTMLTextAreaElement, Props>(function Composer(
  { value, onChange, onSend, pending }, forwardedRef,
) {
  const mobile = useMobile();
  const [quickOpen, setQuickOpen] = useState(false);
  const composing = useRef(false);
  const localRef = useRef<HTMLTextAreaElement | null>(null);
  const id = useId();
  const count = characterCount(value);
  const invalid = count > 500;
  const disabled = pending || !value.trim() || invalid;

  useLayoutEffect(() => {
    const textarea = localRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(Math.max(textarea.scrollHeight, 44), 144)}px`;
  }, [value]);

  function submit() {
    if (!disabled && !composing.current) onSend(value);
  }

  return <footer className="composer-footer shrink-0 bg-canvas px-4 pt-2 sm:px-10 sm:pt-3">
    <div className="mx-auto w-full max-w-chat">
      {mobile && <button type="button" disabled={pending} aria-expanded={quickOpen} aria-controls={`${id}-quick`}
        onClick={() => setQuickOpen(open => !open)} className="mb-1 inline-flex min-h-11 items-center gap-2 rounded-md text-[13px] text-muted disabled:opacity-50">
        常见问题<Chevron open={quickOpen} />
      </button>}
      <div id={`${id}-quick`} aria-label="快捷问题" hidden={mobile && !quickOpen}>
        <div className="-mx-1 mb-2 flex flex-wrap gap-2 px-1">
          {QUICK_QUESTIONS.map(question => <button key={question} type="button" disabled={pending}
            onClick={() => { setQuickOpen(false); onSend(question); }}
            className="group flex min-h-11 min-w-0 max-w-full items-center rounded-full disabled:opacity-50 sm:shrink-0">
            <span className="rounded-full border border-line bg-card px-3 py-1.5 text-left text-[13px] group-hover:border-outline">{question}</span>
          </button>)}
        </div>
      </div>
      <form onSubmit={event => { event.preventDefault(); submit(); }}>
        <div className="flex items-end gap-2 rounded-composer border border-outline bg-card p-2 pl-4 shadow-composer focus-within:border-ink focus-within:ring-1 focus-within:ring-ink sm:gap-3 sm:p-3 sm:pl-5">
          <label htmlFor={id} className="sr-only">输入问题</label>
          <textarea id={id} ref={node => {
            localRef.current = node;
            if (typeof forwardedRef === 'function') forwardedRef(node);
            else if (forwardedRef) forwardedRef.current = node;
          }} rows={1} value={value} disabled={pending} aria-invalid={invalid}
            aria-describedby={`${id}-count`} placeholder={mobile ? '输入你的问题' : '输入你的问题，例如：签收后多久可以退货？'}
            className="max-h-36 min-h-11 min-w-0 flex-1 resize-none bg-transparent py-2.5 text-base leading-6 text-ink outline-none placeholder:text-[13px] placeholder:text-muted disabled:cursor-wait"
            onChange={event => onChange(event.target.value)}
            onCompositionStart={() => { composing.current = true; }}
            onCompositionEnd={() => { composing.current = false; }}
            onKeyDown={event => {
              if (event.key !== 'Enter' || event.shiftKey || composing.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
              event.preventDefault();
              submit();
            }} />
          <button type="submit" aria-label="发送" disabled={disabled}
            className="flex h-11 w-11 shrink-0 items-center justify-center rounded-send bg-accent text-card hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-40"><SendIcon /></button>
        </div>
        <div id={`${id}-count`} aria-live="polite"
          className={`text-right text-xs text-muted ${count > 0 ? 'mt-1 min-h-5' : ''} ${invalid ? 'text-fallback-text' : ''}`}>
          {count > 0 && `${count} / 500${invalid ? ' · 已超出字数限制' : ''}`}
        </div>
      </form>
    </div>
  </footer>;
});
