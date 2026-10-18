import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import type { ChatResponse, ChatStreamEvent } from './api/types';
import { controlledSSE } from './test/streams';

const done: ChatResponse = { request_id: 'r', session_id: 's', answer: '商品说明[1]', route: 'rag',
  strategy: 'direct', sources: [{ index: 1, doc_id: 'd', title: '商品总览', snippet: '商品详情', score: .9 }], is_mock: false, trace: null };

function ask() {
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '你们卖什么' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
}

async function emit(stream: ReturnType<typeof controlledSSE>, event: ChatStreamEvent) {
  await act(async () => { stream.event(event); });
}

describe('streaming UI', () => {
  it('renders incremental text, then enables citations, sources, route and elapsed time only on done', async () => {
    const stream = controlledSSE();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(stream.response));
    render(<App />);
    ask();
    expect(screen.getByText('正在查找常见问题…')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByRole('log')).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('log')).toHaveAttribute('aria-relevant', 'additions');
    await emit(stream, { event: 'meta', data: { request_id: 'r', session_id: 's' } });
    for (const [stage, text] of [['routing', '正在理解问题…'], ['retrieving', '正在查阅店铺资料…'], ['generating', '正在生成回答…']] as const) {
      await emit(stream, { event: 'status', data: { stage } });
      expect(screen.getByText(text)).toBeInTheDocument();
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
    }
    await emit(stream, { event: 'delta', data: { text: '商品' } });
    expect(await screen.findByText('商品')).toBeInTheDocument();
    await emit(stream, { event: 'delta', data: { text: '说明[1]' } });
    expect(await screen.findByText('商品说明[1]')).toBeInTheDocument();
    expect(screen.getByRole('log')).toHaveAttribute('aria-busy', 'true');
    expect(screen.queryByRole('button', { name: '查看引用 1' })).not.toBeInTheDocument();
    expect(screen.queryByText('知识库回答')).not.toBeInTheDocument();
    expect(screen.queryByText('商品总览')).not.toBeInTheDocument();
    await emit(stream, { event: 'done', data: done });
    expect(await screen.findByRole('button', { name: '查看引用 1' })).toBeInTheDocument();
    expect(screen.getByText('商品总览')).toBeInTheDocument();
    expect(screen.getByText('知识库回答')).toBeInTheDocument();
    expect(screen.getByRole('log')).toHaveAttribute('aria-busy', 'false');
    expect(screen.getByText(/\d+\.\d 秒/)).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeEnabled();
  });

  it('shows a stream error bubble and retries with the same session and question', async () => {
    const first = controlledSSE(), second = controlledSSE();
    const fetchMock = vi.fn().mockResolvedValueOnce(first.response).mockResolvedValueOnce(second.response);
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    ask();
    await emit(first, { event: 'meta', data: { request_id: 'r', session_id: 's' } });
    await emit(first, { event: 'delta', data: { text: '部分回答' } });
    expect(await screen.findByText('部分回答')).toBeInTheDocument();
    await emit(first, { event: 'error', data: { code: 50201, message: 'private vendor detail', request_id: 'r' } });
    expect(await screen.findByText('服务有点忙，请稍后再试。')).toBeInTheDocument();
    expect(screen.getByRole('log')).toHaveAttribute('aria-busy', 'false');
    expect(screen.queryByText('private vendor detail')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][1].body).toBe(fetchMock.mock.calls[0][1].body);
    await emit(second, { event: 'done', data: done });
    expect(await screen.findByText('知识库回答')).toBeInTheDocument();
  });

  it('uses done fallback to replace interim text and show the existing support/rephrase actions', async () => {
    vi.stubEnv('VITE_HUMAN_SUPPORT_URL', 'https://support.example/help');
    const stream = controlledSSE();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(stream.response));
    render(<App />);
    ask();
    await emit(stream, { event: 'meta', data: { request_id: 'r', session_id: 's' } });
    await emit(stream, { event: 'done', data: { ...done, route: 'fallback', strategy: null, sources: [], answer: '资料未提及，请联系人工客服。' } });
    expect(await screen.findByText('未找到答案')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: '联系人工客服' })).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: '换个问法' }));
    expect(screen.getByRole('textbox')).toHaveValue('你们卖什么');
    expect(screen.queryByText('商品总览')).not.toBeInTheDocument();
  });

  it('silently falls back to JSON when the stream endpoint is unavailable', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response('missing', { status: 404 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(done), { headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    ask();
    expect(await screen.findByText('知识库回答')).toBeInTheDocument();
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual(['/api/v1/chat/stream', '/api/v1/chat']);
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
  });

  it('does not pull a reader back down on a delta after scrolling up', async () => {
    const stream = controlledSSE();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(stream.response));
    render(<App />);
    ask();
    await emit(stream, { event: 'delta', data: { text: '开头' } });
    const log = screen.getByRole('log');
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 1000 });
    Object.defineProperty(log, 'clientHeight', { configurable: true, value: 200 });
    log.scrollTop = 100;
    fireEvent.scroll(log);
    await emit(stream, { event: 'delta', data: { text: '后续' } });
    expect(log.scrollTop).toBe(100);
    await emit(stream, { event: 'done', data: done });
  });

  it('new conversation aborts the stream and removes in-progress text', async () => {
    const stream = controlledSSE();
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    ask();
    await emit(stream, { event: 'delta', data: { text: '还没写完' } });
    fireEvent.click(screen.getByRole('button', { name: '新对话' }));
    await waitFor(() => expect(stream.response.body?.locked).toBe(false));
    expect(screen.queryByText('还没写完')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeEnabled();
    expect(fetchMock).toHaveBeenCalledOnce();
  });
});
