import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ChatMessage, LoadingMessage, StreamingMessage } from './components/ChatMessage';
import { Composer } from './components/Composer';
import { ConversationHistory, HistoryDrawer } from './components/ConversationHistory';
import { ResizableSidebar } from './components/ResizableSidebar';
import { WelcomeSplash } from './components/WelcomeSplash';
import { useDesktop } from './hooks/useDesktop';
import { Header } from './components/Header';
import { LogoLockup } from './components/Brand';
import { useAutoScroll } from './hooks/useAutoScroll';
import { useMobile } from './hooks/useMobile';
import { useChat } from './hooks/useChat';
import { loadTraceEnabled, saveTraceEnabled, traceToggleVisible } from './lib/storage';
import { humanSupportUrl, storeName } from './lib/config';

export default function App() {
  const mobile = useMobile();
  const desktop = useDesktop();
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
  const { sessionId, conversations, messages, pending, progress, send, reset, selectConversation, deleteConversation, clearConversations } = useChat(showTrace);
  useEffect(() => { saveTraceEnabled(showTrace); }, [showTrace]);
  const [draft, setDraft] = useState('');
  const input = useRef<HTMLTextAreaElement>(null);
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
    if (pending) return;
    setDraft('');
    void send(question);
  }

  function newConversation() {
    reset();
    closeHistory();
    setDraft('');
    scroll.resetFollowing();
    if (scroll.scrollRef.current) scroll.scrollRef.current.scrollTop = 0;
    input.current?.focus();
  }

  function select(id: string) {
    scroll.resetFollowing();
    selectConversation(id);
    if (scroll.scrollRef.current) scroll.scrollRef.current.scrollTop = scroll.scrollRef.current.scrollHeight;
    setDraft('');
    closeHistory();
  }

  const historyProps = { conversations, currentId: sessionId, onNew: newConversation, onSelect: select,
    onDelete: (id: string) => { scroll.resetFollowing(); deleteConversation(id); if (id === sessionId) setDraft(''); },
    onClear: () => { scroll.resetFollowing(); clearConversations(); setDraft(''); } };

  const welcome = <section className={mobile ? 'shrink-0 px-4 pt-6' : 'pb-8 pt-2 sm:py-16'}>
    <LogoLockup className="mb-5 hidden h-14 sm:block" />
    <h1 className="font-serif text-title">你好，我是{name}的智能客服</h1>
    <p className="mt-2 text-sm leading-relaxed text-muted">售后、配送、发票、商品参数，都可以问我。</p>
    {messages.length === 0 && <p className="mt-6 text-[13px] text-muted">从下面的问题开始，或写下你想了解的事。</p>}
  </section>;

  return <div className="app-shell flex bg-canvas font-sans text-ink">
    <WelcomeSplash />
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
            onRetry={(question, id) => { void send(question, id); }}
            onRephrase={question => { setDraft(question); input.current?.focus(); }} />)}
          {pending && (progress?.text ? <StreamingMessage text={progress.text} /> : <LoadingMessage stage={progress?.stage} />)}
        </div>
      </div>
      <Composer ref={input} value={draft} onChange={setDraft} onSend={submit} pending={pending} />
    </main>
    </div>
    {!desktop && historyOpen && <HistoryDrawer {...historyProps} onClose={closeHistory} trigger={historyButton} />}
  </div>;
}
