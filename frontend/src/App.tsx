import { useEffect, useRef, useState } from 'react';
import { ChatMessage, LoadingMessage } from './components/ChatMessage';
import { Composer } from './components/Composer';
import { Header } from './components/Header';
import { useAutoScroll } from './hooks/useAutoScroll';
import { useChat } from './hooks/useChat';
import { humanSupportUrl, storeName } from './lib/config';

export default function App() {
  const { messages, pending, send, reset } = useChat();
  const [draft, setDraft] = useState('');
  const input = useRef<HTMLTextAreaElement>(null);
  const wasPending = useRef(false);
  const scroll = useAutoScroll(`${messages.length}-${pending}`);
  const name = storeName();
  const supportUrl = humanSupportUrl();
  const sample = import.meta.env.VITE_SHOW_SAMPLE_BADGE !== 'false';

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
    setDraft('');
    scroll.resetFollowing();
    if (scroll.scrollRef.current) scroll.scrollRef.current.scrollTop = 0;
    input.current?.focus();
  }

  return <div className="app-shell flex flex-col bg-canvas font-sans text-ink">
    <Header name={name} supportUrl={supportUrl} sample={sample} onReset={newConversation} />
    <main className="flex min-h-0 flex-1 flex-col" aria-label="客服对话">
      <div ref={scroll.scrollRef} onScroll={scroll.onScroll} role="log" aria-label="聊天消息" aria-live="polite" aria-relevant="additions text" tabIndex={0}
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 sm:px-10">
        <div className="mx-auto w-full max-w-chat space-y-8 pb-6 pt-6 sm:pt-10">
          {messages.length === 0 && <section className="pb-8 pt-2 sm:py-16">
            <p className="mb-4 hidden text-xs tracking-widest text-muted sm:block">{name} · 客服中心</p>
            <h1 className="font-serif text-title">你好，我是{name}的智能客服</h1>
            <p className="mt-2 text-sm leading-relaxed text-muted">售后、配送、发票、商品参数，都可以问我。</p>
            <p className="mt-6 text-[13px] text-muted">从下面的问题开始，或写下你想了解的事。</p>
          </section>}
          {messages.map(message => <ChatMessage key={message.id} message={message} pending={pending} supportUrl={supportUrl}
            onRetry={(question, id) => { void send(question, id); }}
            onRephrase={question => { setDraft(question); input.current?.focus(); }} />)}
          {pending && <LoadingMessage />}
        </div>
      </div>
      <Composer ref={input} value={draft} onChange={setDraft} onSend={submit} pending={pending} sample={sample} />
    </main>
  </div>;
}
