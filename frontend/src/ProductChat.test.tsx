import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { conversationsAPI } from './api/account';
import { ChatMessage, StreamingMessage } from './components/ChatMessage';
import { useChat } from './hooks/useChat';
import { CONVERSATIONS_KEY, resetStorageMemory } from './lib/storage';
import { product, productAnswer } from './test/products';
import { controlledSSE } from './test/streams';

// Exercise the real API, hook and message renderer without touching the shared App.
function Chat({ remote = false }: { remote?: boolean }) {
  const chat = useChat(false, { remote });
  const assistant = chat.messages.find(message => message.kind === 'assistant');
  return <>
    <button onClick={() => { void chat.send('台灯规格'); }}>提问</button>
    <button onClick={() => { if (assistant) void chat.send(assistant.question, assistant.id); }}>重新生成</button>
    <button onClick={() => { void chat.selectConversation(chat.sessionId); }}>重新打开历史</button>
    <button onClick={chat.reset}>新对话</button>
    {chat.messages.map(message => <ChatMessage key={message.id} message={message} pending={chat.pending}
      showTrace={false} onRetry={(question, id) => { void chat.send(question, id); }} onRephrase={() => undefined} />)}
    {chat.progress?.text && <StreamingMessage text={chat.progress.text} />}
  </>;
}

const jsonResponse = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
const cards = () => screen.queryByRole('region', { name: '资料关联商品' });

describe('products in live chat and history', () => {
  it('renders streaming text first and reveals cards only on the final event', async () => {
    const stream = controlledSSE();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(stream.response));
    render(<Chat />);
    fireEvent.click(screen.getByRole('button', { name: '提问' }));
    await act(async () => {
      stream.text(`event: meta\ndata: ${JSON.stringify({ request_id: 'r', session_id: 's', products: [product()] })}\n\n`);
      stream.text(`event: delta\ndata: ${JSON.stringify({ text: '正在介绍台灯', products: [product()] })}\n\n`);
    });
    expect(await screen.findByText('正在介绍台灯')).toBeInTheDocument();
    expect(cards()).not.toBeInTheDocument();
    await act(async () => { stream.event({ event: 'done', data: productAnswer([product()]) }); });
    expect(await screen.findByRole('region', { name: '资料关联商品' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: product().name })).toBeInTheDocument();
    expect(screen.queryByText('正在介绍台灯')).not.toBeInTheDocument();
  });

  it.each([false, true])('clears old cards immediately on regeneration and keeps them cleared on retry (remote=%s)', async remote => {
    if (remote) {
      vi.spyOn(conversationsAPI, 'list').mockResolvedValue({ items: [] });
      vi.spyOn(conversationsAPI, 'create').mockResolvedValue({ id: 'server-1', title: '', updated_at: '2026-10-20T00:00:00Z', message_count: 0 });
    }
    const first = controlledSSE(), second = controlledSSE(), third = controlledSSE();
    const fetchMock = vi.fn().mockResolvedValueOnce(first.response).mockResolvedValueOnce(second.response).mockResolvedValueOnce(third.response);
    vi.stubGlobal('fetch', fetchMock);
    render(<Chat remote={remote} />);
    fireEvent.click(screen.getByRole('button', { name: '提问' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await act(async () => { first.event({ event: 'done', data: productAnswer([product()]) }); });
    expect(await screen.findByRole('region', { name: '资料关联商品' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重新生成' }));
    expect(cards()).not.toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).history).toEqual([]);
    await act(async () => { second.event({ event: 'delta', data: { text: '新的部分回答' } }); });
    expect(screen.getByText('新的部分回答')).toBeInTheDocument();
    expect(cards()).not.toBeInTheDocument();
    await act(async () => { second.event({ event: 'error', data: { code: 50201, message: 'failed' } }); });
    fireEvent.click(await screen.findByRole('button', { name: '重试' }));
    expect(cards()).not.toBeInTheDocument();
    await act(async () => { third.event({ event: 'done', data: productAnswer() }); });
    expect(await screen.findByText(productAnswer().answer)).toBeInTheDocument();
    expect(cards()).not.toBeInTheDocument();
  });

  it('renders cards in JSON mode and restores bounded local metadata without sending it as context', async () => {
    vi.stubEnv('VITE_STREAM', '0');
    const fetchMock = vi.fn().mockImplementation(async () => jsonResponse(productAnswer([{ ...product(), price: 99, stock: 1 } as ReturnType<typeof product>])));
    vi.stubGlobal('fetch', fetchMock);
    const first = render(<Chat />);
    fireEvent.click(screen.getByRole('button', { name: '提问' }));
    expect(await screen.findByRole('region', { name: '资料关联商品' })).toBeInTheDocument();
    expect(localStorage.getItem(CONVERSATIONS_KEY)).toContain('qiguang-l1');
    expect(localStorage.getItem(CONVERSATIONS_KEY)).not.toMatch(/"price"|"stock"/);
    first.unmount();
    resetStorageMemory();
    render(<Chat />);
    expect(cards()).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '提问' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    const body = JSON.parse(fetchMock.mock.calls[1][1].body);
    expect(body.history).toEqual([{ role: 'user', content: '台灯规格' }, { role: 'assistant', content: productAnswer().answer }]);
    expect(body).not.toHaveProperty('products');
    expect(JSON.stringify(body.history)).not.toContain('qiguang-l1');
  });

  it('does not invent cards when reopening server history that only saves text, route and sources', async () => {
    vi.stubEnv('VITE_STREAM', '0');
    vi.spyOn(conversationsAPI, 'list').mockResolvedValue({ items: [] });
    vi.spyOn(conversationsAPI, 'create').mockResolvedValue({ id: 'server-1', title: '', updated_at: '2026-10-20T00:00:00Z', message_count: 0 });
    vi.spyOn(conversationsAPI, 'open').mockResolvedValue({ id: 'server-1', title: '台灯规格', messages: [
      { role: 'user', content: '台灯规格', created_at: '2026-10-20T00:00:00Z' },
      { role: 'assistant', content: productAnswer().answer, route: 'rag', sources: [], created_at: '2026-10-20T00:00:01Z' },
    ] });
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => jsonResponse(productAnswer([product()]))));
    render(<Chat remote />);
    fireEvent.click(screen.getByRole('button', { name: '提问' }));
    expect(await screen.findByRole('region', { name: '资料关联商品' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重新打开历史' }));
    await waitFor(() => expect(cards()).not.toBeInTheDocument());
    expect(screen.getByText(productAnswer().answer)).toBeInTheDocument();
    expect(conversationsAPI.open).toHaveBeenCalledWith('server-1', expect.any(AbortSignal));
    expect(localStorage.getItem(CONVERSATIONS_KEY)).toBeNull();
  });
});
