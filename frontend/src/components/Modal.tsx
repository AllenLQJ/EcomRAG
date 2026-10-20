import { useEffect, useRef, type ReactNode } from 'react';

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLDivElement>(null);
  const overlay = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    // Keep the rest of the app out of the keyboard and accessibility trees.
    const siblings = Array.from(overlay.current?.parentElement?.children || []).filter(item => item !== overlay.current);
    const restore = siblings.map(item => ({ item, inert: item.hasAttribute('inert'), hidden: item.getAttribute('aria-hidden') }));
    siblings.forEach(item => { item.setAttribute('inert', ''); item.setAttribute('aria-hidden', 'true'); });
    const controls = () => dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]');
    controls()?.[0]?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); close.current(); }
      if (event.key !== 'Tab') return;
      const items = controls();
      if (!items?.length) return;
      const first = items[0]; const last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.current?.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener('keydown', keydown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', keydown);
      restore.forEach(({ item, inert, hidden }) => {
        if (!inert) item.removeAttribute('inert');
        if (hidden === null) item.removeAttribute('aria-hidden'); else item.setAttribute('aria-hidden', hidden);
      });
      if (previousFocus?.isConnected) previousFocus.focus();
    };
  }, []);
  return <div ref={overlay} className="fixed inset-0 z-[60] overflow-y-auto bg-ink/30 p-4 sm:p-10">
    <div ref={dialog} role="dialog" aria-modal="true" aria-label={title} className="mx-auto w-full max-w-chat rounded-card border border-line bg-canvas p-4 sm:p-8">
      <div className="mb-4 flex items-center justify-between gap-4">
        <h2 className="font-serif text-title">{title}</h2>
        <button type="button" className="secondary-button shrink-0" onClick={onClose} aria-label={`关闭${title}`}>关闭</button>
      </div>
      {children}
    </div>
  </div>;
}
