import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ChatMessage, LoadingMessage, StreamingMessage } from './components/ChatMessage';
import { Composer } from './components/Composer';
import { ConversationHistory, HistoryDrawer } from './components/ConversationHistory';
import { ResizableSidebar } from './components/ResizableSidebar';
import { AuthPanel } from './components/AuthPanel';
import { Modal } from './components/Modal';
import { DeleteAccount } from './components/DeleteAccount';
import { ShopInfo } from './components/ShopInfo';
import { accountAPI, accountError, guestIdentity, type Identity, type User } from './api/account';
import { WELCOME_KEY, WelcomeSplash } from './components/WelcomeSplash';
import { useDesktop } from './hooks/useDesktop';
import { Header } from './components/Header';
import { LogoLockup } from './components/Brand';
import { useAutoScroll } from './hooks/useAutoScroll';
import { useMobile } from './hooks/useMobile';
import { useChat } from './hooks/useChat';
import { loadTraceEnabled, saveTraceEnabled, traceToggleVisible } from './lib/storage';
import { humanSupportUrl, storeName } from './lib/config';

export const GUEST_ENTRY_KEY = 'lingma.entry.guest.v1';
function guestSelected(): boolean {
  try { return sessionStorage.getItem(GUEST_ENTRY_KEY) === '1'; } catch { return false; }
}
function resetWelcome() { try { sessionStorage.removeItem(WELCOME_KEY); } catch { /* Optional storage. */ } }

export default function App() {
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [entry, setEntry] = useState(true);
  const [authOpen, setAuthOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [playWelcome, setPlayWelcome] = useState(true);
  const [notice, setNotice] = useState('');
  const [loggingOut, setLoggingOut] = useState(false);
  const authEpoch = useRef(0);
  const quotaEpoch = useRef(0);
  const name = storeName();
  useEffect(() => { document.title = `${name} · 客服中心`; }, [name]);
  useEffect(() => {
    const controller = new AbortController();
    accountAPI.me(controller.signal).then(data => {
      if (controller.signal.aborted) return;
      setIdentity(data); setEntry(!data.user && !guestSelected());
    }).catch(() => {
      if (controller.signal.aborted) return;
      setIdentity(guestIdentity()); setEntry(!guestSelected());
      setNotice('暂时无法获取身份与配额，你仍可先以访客进入。');
    });
    return () => controller.abort();
  }, []);

  const refreshQuota = useCallback(() => {
    const generation = authEpoch.current;
    const request = ++quotaEpoch.current;
    void accountAPI.me().then(data => {
      if (generation !== authEpoch.current || request !== quotaEpoch.current) return;
      setIdentity(data);
      setNotice('');
    }).catch(() => { if (generation === authEpoch.current && request === quotaEpoch.current) setNotice('配额暂时无法更新，请稍后再试。'); });
  }, []);

  function enterGuest() {
    try { sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); } catch { /* Entry still works in memory. */ }
    resetWelcome(); setPlayWelcome(true); setEntry(false);
  }
  function authenticated(user: User) {
    authEpoch.current += 1;
    if (entry) resetWelcome();
    setPlayWelcome(entry);
    setIdentity({ user, quota: { role: 'user', limit: 50, used: 0, remaining: 50, resets_at: '' } });
    setEntry(false); setAuthOpen(false); setNotice(''); refreshQuota();
  }
  function signedOut() {
    authEpoch.current += 1;
    try { sessionStorage.removeItem(GUEST_ENTRY_KEY); } catch { /* Optional storage. */ }
    resetWelcome(); setIdentity(guestIdentity()); setEntry(true); setAuthOpen(false); setDeleteOpen(false); setNotice('');
  }
  async function logout() {
    if (loggingOut) return;
    setLoggingOut(true);
    try { await accountAPI.logout(); signedOut(); }
    catch (error) { setNotice(accountError(error)); }
    finally { setLoggingOut(false); }
  }
  if (!identity) return <main className="app-shell flex items-center justify-center bg-canvas text-sm text-muted" aria-label="正在获取身份">正在连接客服中心…</main>;
  if (entry) return <main aria-label="入口登录页" className="entry-page">
    <div className="w-full max-w-[360px]">
      <AuthPanel onSuccess={authenticated} onGuest={enterGuest} />
      {notice && <p role="status" className="mt-4 text-sm text-muted">{notice}</p>}
    </div>
  </main>;
  return <>
    <ChatHome key={identity.user ? `user-${identity.user.id}` : 'guest'} identity={identity} onQuotaRefresh={refreshQuota}
      playWelcome={playWelcome} notice={notice} loggingOut={loggingOut} onLogin={() => setAuthOpen(true)}
      onLogout={() => { void logout(); }} onDeleteAccount={() => setDeleteOpen(true)} />
    {authOpen && <Modal title="登录/注册" auth onClose={() => setAuthOpen(false)}><AuthPanel onSuccess={authenticated} /></Modal>}
    {deleteOpen && <DeleteAccount onClose={() => setDeleteOpen(false)} onDelete={async password => { await accountAPI.remove(password); signedOut(); }} />}
  </>;
}

interface HomeProps {
  identity?: Identity; onQuotaRefresh?: () => void; playWelcome?: boolean; notice?: string; loggingOut?: boolean;
  onLogin?: () => void; onLogout?: () => void; onDeleteAccount?: () => void;
}

export function ChatHome({ identity = guestIdentity(), onQuotaRefresh, playWelcome = true, notice, loggingOut,
  onLogin, onLogout, onDeleteAccount }: HomeProps = {}) {
  const mobile = useMobile();
  const desktop = useDesktop();
  const [shopOpen, setShopOpen] = useState(false);
  const [quotaScope, setQuotaScope] = useState<'guest' | 'user' | undefined>();
  const quotaExceeded = useCallback((scope?: 'guest' | 'user') => setQuotaScope(scope || (identity.user ? 'user' : 'guest')), [identity.user]);
  useEffect(() => { if (identity.quota.remaining > 0) setQuotaScope(undefined); }, [identity.quota]);
  const exhausted = identity.quota.remaining === 0 || Boolean(quotaScope);
  const [historyOpen, setHistoryOpen] = useState(false);
  const historyButton = useRef<HTMLButtonElement>(null);
  const chatArea = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const area = chatArea.current;
    if (historyOpen) area?.setAttribute('inert', '');
    else area?.removeAttribute('inert');
    return () => area?.removeAttribute('inert');
  }, [historyOpen]);
  const closeHistory = useCallback(() => setHistoryOpen(false), []);
  useEffect(() => { if (desktop) setHistoryOpen(false); }, [desktop]);
  const traceVisible = traceToggleVisible();
  const [traceEnabled, setTraceEnabled] = useState(() => traceVisible && loadTraceEnabled());
  const showTrace = traceVisible && traceEnabled;
  const { sessionId, conversations, messages, pending, progress, send, reset, selectConversation, deleteConversation, renameConversation, historyError, refreshConversations } = useChat(showTrace, { remote: Boolean(identity.user), onSettled: onQuotaRefresh, onQuotaExceeded: quotaExceeded });
  useEffect(() => { saveTraceEnabled(showTrace); }, [showTrace]);
  const [draft, setDraft] = useState('');
  const input = useRef<HTMLTextAreaElement>(null);
  const draftEpoch = useRef(0);
  const wasPending = useRef(false);
  const scroll = useAutoScroll(`${sessionId}-${messages.length}-${pending}-${progress?.text.length || 0}-${progress?.stage || ""}`);
  const name = storeName();
  const supportUrl = humanSupportUrl();

  useEffect(() => { document.title = `${name} · 客服中心`; }, [name]);
  useEffect(() => {
    // Restore keyboard focus when sending completes, unless the reader moved elsewhere.
    const completed = wasPending.current && !pending;
    wasPending.current = pending;
    if (completed && (document.activeElement === document.body || document.activeElement === input.current)) input.current?.focus();
  }, [pending]);

  function submit(question: string) {
    if (pending || exhausted) return;
    const generation = draftEpoch.current;
    setDraft('');
    void send(question).then(sent => { if (!sent && generation === draftEpoch.current) setDraft(previous => previous || question); });
  }

  function newConversation() {
    draftEpoch.current += 1;
    reset();
    closeHistory();
    setDraft('');
    scroll.resetFollowing();
    if (scroll.scrollRef.current) scroll.scrollRef.current.scrollTop = 0;
    input.current?.focus();
  }

  function select(id: string) {
    scroll.resetFollowing();
    const selected = selectConversation(id);
    if (selected instanceof Promise) {
      void selected.then(ok => { if (ok) { draftEpoch.current += 1; setDraft(''); closeHistory(); } });
      return;
    }
    draftEpoch.current += 1;
    if (scroll.scrollRef.current) scroll.scrollRef.current.scrollTop = scroll.scrollRef.current.scrollHeight;
    setDraft('');
    closeHistory();
  }

  const historyProps = { conversations, currentId: sessionId, onNew: newConversation, onSelect: select,
    onDelete: (id: string) => {
      scroll.resetFollowing();
      if (id === sessionId) { draftEpoch.current += 1; if (!identity.user) setDraft(''); }
      void deleteConversation(id);
    },
    onRename: identity.user ? renameConversation : undefined,
    footer: onLogin && <div className="mt-3 border-t border-line pt-3 text-sm">
      <p className="break-words font-medium">{identity.user?.username || '游客'}{!identity.user && ' ·'}<span className="text-muted">{!identity.user && ` 今日剩余 ${identity.quota.remaining}/${identity.quota.limit} 次`}</span></p>
      {identity.user ? <><p className="mt-1 text-xs text-muted">今日剩余 {identity.quota.remaining}/{identity.quota.limit} 次</p>
        <div className="flex flex-wrap gap-2"><button type="button" className="min-h-11 text-muted" disabled={loggingOut} onClick={onLogout}>{loggingOut ? '正在退出…' : '退出登录'}</button>
          <button type="button" className="min-h-11 text-muted" onClick={() => { closeHistory(); onDeleteAccount?.(); }}>注销账号</button></div></>
        : <button type="button" className="secondary-button mt-3 w-full" onClick={() => { closeHistory(); onLogin(); }}>登录/注册</button>}
    </div> };

  const welcome = <section className={mobile ? 'shrink-0 px-4 pt-6' : 'pb-8 pt-2 sm:py-16'}>
    <button type="button" aria-label="店铺信息" className="shop-entry" onClick={() => setShopOpen(true)}>
      <span><span className="block text-title font-semibold">店铺信息</span><span className="mt-1 block text-sm text-muted">了解店铺、服务承诺与商品分类</span></span>
      <span aria-hidden="true" className="text-title">↗</span>
    </button>
    <LogoLockup className="mb-5 hidden h-14 sm:block" />
    <h1 className="font-serif text-title">你好，我是{name}的智能客服</h1>
    <p className="mt-2 text-sm leading-relaxed text-muted">售后、配送、发票、商品参数，都可以问我。</p>
    {messages.length === 0 && <p className="mt-6 text-[13px] text-muted">从下面的问题开始，或写下你想了解的事。</p>}
  </section>;

  return <div className="app-shell flex bg-canvas font-sans text-ink">
    {playWelcome && <WelcomeSplash />}
    {desktop && <ResizableSidebar><ConversationHistory {...historyProps} /></ResizableSidebar>}
    <div className="flex min-w-0 flex-1 flex-col">
    <Header desktop={desktop} historyOpen={historyOpen} onHistory={() => setHistoryOpen(true)} historyButton={historyButton} name={name} supportUrl={supportUrl} onReset={newConversation}
      traceVisible={traceVisible} traceEnabled={showTrace} onToggleTrace={() => setTraceEnabled(enabled => !enabled)} />
    <main ref={chatArea} aria-hidden={historyOpen || undefined} className="flex min-h-0 flex-1 flex-col" aria-label="客服对话">
      {mobile && messages.length === 0 && welcome}
      <div ref={scroll.scrollRef} onScroll={scroll.onScroll} role="log" aria-label="聊天消息" aria-live="polite" aria-busy={pending} aria-relevant="additions" tabIndex={0}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 sm:px-10">
        <div className="mx-auto w-full max-w-chat space-y-8 pb-6 pt-6 sm:pt-10">
          {!mobile && messages.length === 0 && welcome}
          {messages.map(message => <ChatMessage key={message.id} message={message} pending={pending} showTrace={showTrace} supportUrl={supportUrl}
            onLogin={onLogin} quotaRole={identity.user ? 'user' : 'guest'} onRetry={(question, id) => { if (!exhausted) void send(question, id); }}
            onRephrase={question => { setDraft(question); input.current?.focus(); }} />)}
          {pending && (progress?.text ? <StreamingMessage text={progress.text} /> : <LoadingMessage stage={progress?.stage} />)}
        </div>
      </div>
      {(notice || historyError) && <div className="mx-auto w-full max-w-chat px-4 py-2 text-sm text-fallback-text" role="status">
        {notice && <p>{notice}</p>}{historyError && <><p>{historyError}</p><button type="button" className="min-h-11 underline" onClick={() => { void refreshConversations(); }}>刷新历史列表</button></>}
      </div>}
      {exhausted && <div role="status" className="mx-auto w-full max-w-chat px-4 py-2 text-sm text-fallback-text">
        {(quotaScope || identity.quota.role) === 'guest' ? <>访客今日次数已用完，登录后每天可提问 50 次。<button type="button" className="secondary-button ml-2" onClick={onLogin}>登录/注册</button></> : '今日提问次数已用完，请明天再试。'}
      </div>}
      <Composer ref={input} blocked={exhausted} value={draft} onChange={setDraft} onSend={submit} pending={pending} />
    </main>
    </div>
    {shopOpen && <ShopInfo onClose={() => setShopOpen(false)} onCategory={question => { setShopOpen(false); setDraft(question); window.setTimeout(() => input.current?.focus(), 0); }} />}
    {!desktop && historyOpen && <HistoryDrawer {...historyProps} onClose={closeHistory} trigger={historyButton} />}
  </div>;
}
