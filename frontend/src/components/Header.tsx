import type { RefObject } from 'react';
import { LogoMark } from './Brand';

interface Props { desktop: boolean; historyOpen: boolean; onHistory: () => void; historyButton: RefObject<HTMLButtonElement>; name: string; supportUrl?: string; onReset: () => void; traceVisible: boolean; traceEnabled: boolean; onToggleTrace: () => void }

export function Header({ desktop, historyOpen, onHistory, historyButton, name, supportUrl, onReset, traceVisible, traceEnabled, onToggleTrace }: Props) {
  return <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-line bg-card px-4 sm:h-16 sm:px-10">
    <div className="flex min-w-0 items-center gap-2 sm:gap-3">
      {!desktop && <button ref={historyButton} type="button" aria-label="历史" aria-expanded={historyOpen} aria-controls={historyOpen ? 'history-drawer' : undefined}
        onClick={onHistory} className="flex min-h-11 min-w-11 items-center justify-center rounded-card text-muted hover:text-ink">
        <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M4 4h16v16H4zM9 4v16m4-11h4m-4 4h4" /></svg>
      </button>}
      <LogoMark className="h-8 w-8" />
      <div className="flex min-w-0 items-baseline gap-2 sm:gap-3">
        <span className="truncate font-serif text-base font-semibold sm:text-lg">{name}<span className="sm:hidden">客服</span></span>
        <span className="hidden text-sm text-muted sm:inline">客服中心</span>
      </div>
    </div>
    <div className="flex shrink-0 items-center gap-1 sm:gap-3">
      {traceVisible && <button type="button" aria-pressed={traceEnabled} onClick={onToggleTrace}
        className="min-h-11 rounded-full px-2 text-xs text-muted hover:text-ink aria-pressed:bg-line aria-pressed:text-ink sm:px-3">查看链路</button>}
      {!desktop && <button type="button" aria-label="新对话" onClick={onReset} className="min-h-11 rounded-full px-2 text-xs text-muted hover:text-ink sm:px-3 sm:text-sm">新对话</button>}
      {supportUrl && <a href={supportUrl} target="_blank" rel="noopener noreferrer" aria-label="联系人工客服" className="inline-flex min-h-11 items-center rounded-full px-2 text-xs sm:border sm:border-outline sm:px-4 sm:text-sm">
        <span className="sm:hidden">人工客服</span><span className="hidden sm:inline">联系人工客服</span>
      </a>}
    </div>
  </header>;
}
