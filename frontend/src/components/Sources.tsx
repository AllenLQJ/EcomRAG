import { useEffect, useId, useRef, useState } from 'react';
import type { Source } from '../api/types';
import { Chevron } from './Brand';

interface Props {
  sources: Source[];
  selection: { index: number; sequence: number } | null;
}

export function Sources({ sources, selection }: Props) {
  const [open, setOpen] = useState(true);
  const [highlighted, setHighlighted] = useState<number | null>(null);
  const cards = useRef(new Map<number, HTMLElement>());
  const id = useId();

  useEffect(() => {
    if (!selection) return;
    setOpen(true);
    setHighlighted(selection.index);
  }, [selection]);

  useEffect(() => {
    if (!open || highlighted === null) return;
    const card = cards.current.get(highlighted);
    card?.scrollIntoView?.({ block: 'nearest', behavior: 'auto' });
    card?.focus({ preventScroll: true });
    const timer = setTimeout(() => setHighlighted(null), 2400);
    return () => clearTimeout(timer);
  }, [open, highlighted, selection]);

  if (sources.length === 0) return null;
  return <section aria-label="引用来源">
    <button type="button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(value => !value)}
      className="inline-flex min-h-11 items-center gap-2 rounded-md text-[13px] text-muted">
      引用来源 {sources.length}<Chevron open={open} />
    </button>
    <div id={id} hidden={!open}>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {sources.map((source, position) => <article key={`${source.index}-${position}`} tabIndex={-1}
          aria-label={`引用 ${source.index}：${source.title}`}
          ref={node => { if (node) cards.current.set(source.index, node); else cards.current.delete(source.index); }}
          className={`min-w-0 scroll-m-4 rounded-card border bg-card px-4 py-3 outline-none ${highlighted === source.index ? 'border-accent ring-2 ring-accent' : 'border-line'} focus-visible:ring-2 focus-visible:ring-accent`}>
          <h3 className="flex items-baseline gap-2 text-[13px] font-medium"><span className="shrink-0 font-mono text-xs text-accent">[{source.index}]</span>{source.title}</h3>
          <p className="mt-1.5 line-clamp-3 whitespace-pre-line break-words text-sm leading-relaxed text-body-muted">{source.snippet}</p>
        </article>)}
      </div>
    </div>
  </section>;
}
