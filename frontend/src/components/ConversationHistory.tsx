import { useCallback, useEffect, useRef, useState, type ReactNode, type RefObject } from 'react';
import type { Conversation } from '../lib/storage';
import { groupConversations, relativeTime } from '../lib/conversationTime';
import { useConversationClock } from '../hooks/useConversationClock';

interface Props {
  conversations: Conversation[];
  currentId: string;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRename?: (id: string, title: string) => Promise<boolean>;
  footer?: ReactNode;
}

export function ConversationHistory({ conversations, currentId, onNew, onSelect, onDelete, onRename, footer }: Props) {
  const [confirmation, setConfirmation] = useState<{ id: string } | null>(null);
  const [editing, setEditing] = useState<{ id: string; title: string } | null>(null);
  const [renaming, setRenaming] = useState(false);
  const renamePending = useRef(false);
  const now = useConversationClock();
  const nav = useRef<HTMLElement>(null);
  useEffect(() => { setConfirmation(null); setEditing(null); }, [currentId]);
  const focusNew = useCallback(() => { nav.current?.querySelector<HTMLButtonElement>('button')?.focus(); }, []);
  useEffect(() => {
    if (!confirmation) return;
    const cancel = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      setConfirmation(null);
      focusNew();
    };
    // Capture before the drawer's Escape handler, including when focus is on its close button.
    document.addEventListener('keydown', cancel, true);
    return () => document.removeEventListener('keydown', cancel, true);
  }, [confirmation, focusNew]);
  return <nav ref={nav} aria-label="历史对话" className="flex h-full min-h-0 flex-col bg-canvas p-4">
    <button type="button" className="secondary-button w-full shrink-0" aria-label="新对话" onClick={onNew}>＋ 新对话</button>
    <div className="mt-6 min-h-0 flex-1 overflow-y-auto px-1 pb-4">
      {!conversations.length && <p className="py-4 text-sm text-muted">还没有历史对话</p>}
      {groupConversations(conversations, now).filter(group => group.conversations.length).map(group => <section key={group.label} className="mb-6">
        <h2 className="mb-2 px-2 text-xs text-muted">{group.label}</h2>
        <ul aria-label={`${group.label}的对话`} className="space-y-1">
          {group.conversations.map(item => <li key={item.id} className="group rounded-card">
            <div className={`flex items-center rounded-card ${currentId === item.id ? 'bg-line' : 'hover:bg-card'}`}>
              <button type="button" aria-label={`打开对话：${item.title}`} aria-current={currentId === item.id ? 'page' : undefined}
                onClick={() => { setConfirmation(null); onSelect(item.id); }} className="min-h-11 min-w-0 flex-1 rounded-card px-2 py-2 text-left">
                <span className="block truncate text-sm">{item.title}</span>
                <time dateTime={new Date(item.updatedAt).toISOString()} className="mt-1 block text-xs text-muted">{relativeTime(item.updatedAt, now)}</time>
              </button>
              {onRename && <button type="button" aria-label={`重命名对话：${item.title}`} className="history-rename min-h-11 min-w-11 rounded-card text-xs text-muted"
                onClick={() => { setConfirmation(null); setEditing({ id: item.id, title: item.title }); }}>改名</button>}
              <button type="button" aria-label={`删除对话：${item.title}`} onClick={() => setConfirmation({ id: item.id })}
                className="history-delete min-h-11 min-w-11 rounded-card text-muted hover:text-ink">
                <svg aria-hidden="true" viewBox="0 0 24 24" className="mx-auto h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7m4-7v7" /></svg>
              </button>
            </div>
            {editing?.id === item.id && <form className="space-y-2 rounded-card border border-outline bg-card p-2" onSubmit={async event => {
              event.preventDefault();
              if (!onRename || renamePending.current || !editing.title.trim()) return;
              renamePending.current = true; setRenaming(true);
              try { if (await onRename(item.id, editing.title)) { setEditing(null); focusNew(); } }
              finally { renamePending.current = false; setRenaming(false); }
            }}>
              <label className="block text-xs" htmlFor={`rename-${item.id}`}>对话标题</label>
              <input autoFocus id={`rename-${item.id}`} className="form-input" value={editing.title} disabled={renaming}
                onChange={event => setEditing({ id: item.id, title: event.target.value })}
                onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setEditing(null); focusNew(); } }} />
              <div className="flex flex-wrap gap-2"><button type="submit" className="secondary-button" disabled={renaming || !editing.title.trim()}>保存标题</button>
                <button type="button" className="min-h-11 px-2 text-sm" onClick={() => setEditing(null)}>取消改名</button></div>
            </form>}
            {confirmation?.id === item.id && <div role="group" aria-label={`确认删除：${item.title}`} className="rounded-card border border-outline bg-card p-2 text-xs">
              <p>删除这段对话？</p>
              <div className="flex gap-1">
                <button type="button" aria-label="取消" autoFocus className="min-h-11 rounded-card px-3" onClick={() => { setConfirmation(null); focusNew(); }}>取消</button>
                <button type="button" aria-label="确认删除" className="min-h-11 rounded-card px-3 font-medium" onClick={() => { setConfirmation(null); onDelete(item.id); focusNew(); }}>确认删除</button>
              </div>
            </div>}
          </li>)}
        </ul>
      </section>)}
    </div>
    {footer && <div className="shrink-0">{footer}</div>}
  </nav>;
}

interface DrawerProps extends Props { onClose: () => void; trigger: RefObject<HTMLButtonElement> }

export function HistoryDrawer({ onClose, trigger, ...props }: DrawerProps) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.key === 'Escape') { event.preventDefault(); onClose(); }
      if (event.key !== 'Tab') return;
      const controls = dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [href], [tabindex="0"]');
      if (!controls?.length) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || !dialog.current?.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.current?.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener('keydown', keydown);
    const triggerElement = trigger.current;
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener('keydown', keydown);
      triggerElement?.focus();
    };
  }, [onClose, trigger]);
  return <div className="fixed inset-0 z-50 flex">
    <button type="button" aria-label="关闭历史遮罩" tabIndex={-1} onClick={onClose} className="absolute inset-0 bg-ink/30" />
    <div ref={dialog} id="history-drawer" role="dialog" aria-modal="true" aria-label="历史对话抽屉"
      className="relative flex h-full w-[280px] max-w-[85vw] flex-col border-r border-line bg-canvas">
      <button type="button" aria-label="关闭历史" onClick={onClose} className="mx-4 mt-2 min-h-11 shrink-0 self-end rounded-card px-3 text-sm text-muted">关闭</button>
      <div className="min-h-0 flex-1"><ConversationHistory {...props} /></div>
    </div>
  </div>;
}
