import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatHome as App } from './App';
import type { ChatResponse, TraceItem } from './api/types';
import { fixtureFor } from './mocks/fixtures';
import { mockMobile, mockViewportWidth } from './test/media';
import { CONVERSATIONS_KEY, SESSION_KEY, TRACE_KEY } from './lib/storage';

function result(question = '划痕', debug = false): ChatResponse {
  return { ...fixtureFor({ session_id: 'server-session', message: question, debug }), is_mock: false };
}

function json(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

async function ask(question: string) {
  const user = userEvent.setup();
  await user.type(screen.getByRole('textbox', { name: '输入问题' }), question);
  await user.click(screen.getByRole('button', { name: '发送' }));
  return user;
}

describe('chat flow', () => {
  // Preserve the compatibility endpoint regression suite; streaming has its own tests.
  beforeEach(() => { vi.stubEnv('VITE_STREAM', '0'); });
  it('shows the welcome and quick questions without footer notices', () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: '你好，我是灵码的智能客服' })).toBeInTheDocument();
    expect(within(screen.getByLabelText('快捷问题')).getAllByRole('button')).toHaveLength(4);
    expect(screen.queryByText('回答由 AI 根据店铺资料生成，仅供参考')).not.toBeInTheDocument();
    expect(screen.queryByText('样例数据')).not.toBeInTheDocument();
    expect(screen.queryByText('对话内容会被记录，用于改进服务')).not.toBeInTheDocument();
    expect(screen.queryByText('Enter 发送 · Shift + Enter 换行')).not.toBeInTheDocument();
    expect(screen.queryByText('历史仅保存在本浏览器')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '联系人工客服' })).not.toBeInTheDocument();
  });

  it('renders RAG Markdown and citations; keyboard navigation opens and highlights sources', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(result())));
    render(<App />);
    const user = await ask('屏幕有划痕可以退吗？');
    expect(await screen.findByText('知识库回答')).toBeInTheDocument();
    expect(screen.getByText('7 天内').tagName).toBe('STRONG');
    expect(within(screen.getByRole('log')).getByRole('list')).toBeInTheDocument();
    const toggle = screen.getByRole('button', { name: '引用来源 3' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    screen.getByRole('button', { name: '查看引用 2' }).focus();
    await user.keyboard('{Enter}');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const card = screen.getByRole('article', { name: '引用 2：售后政策 · 申请材料' });
    expect(card).toHaveFocus();
    expect(card).toHaveClass('ring-2');
    expect(card.scrollIntoView).toHaveBeenCalled();
    expect(screen.queryByText('after_sales_policy.md')).not.toBeInTheDocument();
    expect(screen.queryByText('return_guide.md')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('');
    expect(screen.getByRole('log')).toHaveAttribute('aria-live', 'polite');
    expect(screen.getAllByText(/\d+\.\d 秒/).length).toBeGreaterThan(0);
  });

  it('quick questions send FAQ requests without source filename labels', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(result('怎么申请退货？')));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    await userEvent.setup().click(screen.getByRole('button', { name: '怎么申请退货？' }));
    await screen.findByRole('button', { name: '引用来源 1' });
    const source = screen.getByRole('article', { name: '引用 1：怎么申请退货？' });
    expect(within(source).queryByText('常见问题')).not.toBeInTheDocument();
    expect(screen.queryByText('faq:12')).not.toBeInTheDocument();
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ message: '怎么申请退货？', debug: false });
  });

  it('fallback offers support and rephrasing, restoring the original question and focus', async () => {
    vi.stubEnv('VITE_HUMAN_SUPPORT_URL', 'https://support.example.test/help');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(result('寄到火星吗？'))));
    render(<App />);
    const user = await ask('寄到火星吗？');
    expect(await screen.findByText('未找到答案')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: '联系人工客服' })).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: '换个问法' }));
    expect(screen.getByRole('textbox')).toHaveValue('寄到火星吗？');
    expect(screen.getByRole('textbox')).toHaveFocus();
    expect(screen.queryByRole('button', { name: /引用来源/ })).not.toBeInTheDocument();
  });

  it('hides support links when no URL is configured, including in fallback', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(result('火星'))));
    render(<App />);
    await ask('火星');
    expect(await screen.findByRole('button', { name: '换个问法' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '联系人工客服' })).not.toBeInTheDocument();
  });

  it('hides the retained request ID; retry resends the same question and session', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ code: 42901, message: 'private detail', request_id: 'req-123' }, false))
      .mockResolvedValueOnce(json(result('怎么申请退货？')));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    const user = await ask('怎么申请退货？');
    expect(await screen.findByText('提问有点快，休息一分钟再问吧。')).toBeInTheDocument();
    expect(screen.queryByText(/请求编号|req-123/)).not.toBeInTheDocument();
    expect(localStorage.getItem(CONVERSATIONS_KEY)).toBeNull();
    await user.click(screen.getByRole('button', { name: '重试' }));
    await screen.findByRole('button', { name: '引用来源 1' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][1].body).toBe(fetchMock.mock.calls[0][1].body);
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
    expect(screen.getAllByText('怎么申请退货？', { selector: 'p' })).toHaveLength(1);
  });

  it('new conversation clears the messages and changes session_id', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(result('你好')));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    const user = await ask('你好');
    await screen.findByText('闲聊');
    const oldId = localStorage.getItem(SESSION_KEY);
    await user.click(screen.getByRole('button', { name: '新对话' }));
    expect(localStorage.getItem(SESSION_KEY)).not.toBe(oldId);
    expect(screen.queryByText('闲聊')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /你好，我是/ })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem(CONVERSATIONS_KEY)!)).toHaveLength(1);
    expect(screen.getByRole('button', { name: '打开对话：你好' })).toBeInTheDocument();
    await ask('你好');
    await screen.findByText('闲聊');
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).session_id).not.toBe(JSON.parse(fetchMock.mock.calls[0][1].body).session_id);
  });

  it('disables input while pending and discards an old result after reset', async () => {
    let resolve!: (value: Response) => void;
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>(done => { resolve = done; }));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    const user = await ask('划痕');
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
    expect(screen.getByText('正在查找常见问题…')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '新对话' }));
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => { resolve(json(result())); });
    expect(screen.queryByText('知识库回答')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeEnabled();
  });

  it('does not force the reader back to the bottom after they scroll up', async () => {
    let resolve!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise<Response>(done => { resolve = done; })));
    render(<App />);
    await ask('划痕');
    const log = screen.getByRole('log');
    Object.defineProperties(log, { scrollHeight: { configurable: true, value: 1200 }, clientHeight: { configurable: true, value: 400 } });
    log.scrollTop = 200;
    fireEvent.scroll(log);
    await act(async () => { resolve(json(result())); });
    await screen.findByText('知识库回答');
    expect(log.scrollTop).toBe(200);
  });

  it('restores the current tab conversation on remount', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(result('你好')));
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<App />);
    await ask('你好');
    await screen.findByText('闲聊');
    view.unmount();
    render(<App />);
    expect(screen.getByText('闲聊')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('continues sending and resetting when storage throws', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('blocked'); });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(result('你好'))));
    render(<App />);
    const user = await ask('你好');
    expect(await screen.findByText('闲聊')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '新对话' }));
    expect(screen.getByRole('heading', { name: /你好，我是/ })).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeEnabled();
  });

  it.each([
    ['null', null], ['empty', []], ['nonempty', [{ stage: 'retrieve', ms: 120, candidates: 10 }]],
  ] as [string, TraceItem[] | null][])('shows trace only for a nonempty array (%s)', async (_label, trace) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...result('你好'), trace })));
    vi.stubEnv('VITE_SHOW_TRACE_TOGGLE', '1');
    render(<App />);
    await userEvent.setup().click(screen.getByRole('button', { name: '查看链路' }));
    await ask('你好');
    await screen.findByText('闲聊');
    if (trace?.length) {
      const summary = screen.getByText('链路 · 1 个阶段');
      expect(summary.closest('details')).not.toHaveAttribute('open');
      await userEvent.setup().click(summary);
      expect(screen.getByText('120 ms')).toBeVisible();
      expect(screen.getByText('候选数: 10')).toBeVisible();
    } else expect(screen.queryByText(/链路 ·/)).not.toBeInTheDocument();
  });

  it.each(['url', 'storage', 'build'])('requires an explicit toggle before requesting debug via %s', async origin => {
    if (origin === 'url') window.history.replaceState({}, '', '/?debug=1');
    else if (origin === 'storage') localStorage.setItem('ecomrag.debug', '1');
    else vi.stubEnv('VITE_SHOW_TRACE_TOGGLE', '1');
    const fetchMock = vi.fn().mockResolvedValue(json(result('你好')));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    await ask('你好');
    await screen.findByText('闲聊');
    const toggle = screen.getByRole('button', { name: '查看链路' });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).debug).toBe(false);
    await userEvent.setup().click(toggle);
    expect(toggle).toHaveAttribute('aria-pressed', 'true');
    await ask('再问一次');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).debug).toBe(true);
  });
  it('hides all trace UI for ordinary users, including server-supplied and restored traces', async () => {
    sessionStorage.setItem(TRACE_KEY, '1');
    const fetchMock = vi.fn().mockResolvedValue(json(result('划痕', true)));
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<App />);
    expect(screen.queryByRole('button', { name: '查看链路' })).not.toBeInTheDocument();
    await ask('划痕');
    await screen.findByText('知识库回答');
    expect(screen.queryByText(/链路 ·/)).not.toBeInTheDocument();
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).debug).toBe(false);
    view.unmount();
    render(<App />);
    expect(screen.queryByText(/链路 ·/)).not.toBeInTheDocument();
  });

  it('persists the trace switch in the current tab and hides existing panels when switched off', async () => {
    vi.stubEnv('VITE_SHOW_TRACE_TOGGLE', '1');
    const fetchMock = vi.fn().mockResolvedValue(json(result('划痕', true)));
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<App />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '查看链路' }));
    expect(sessionStorage.getItem(TRACE_KEY)).toBe('1');
    await ask('划痕');
    await screen.findByText('链路 · 6 个阶段');
    view.unmount();
    render(<App />);
    expect(screen.getByRole('button', { name: '查看链路' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('链路 · 6 个阶段')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看链路' }));
    expect(screen.queryByText(/链路 ·/)).not.toBeInTheDocument();
    expect(sessionStorage.getItem(TRACE_KEY)).toBe('0');
    await ask('继续');
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(fetchMock.mock.calls[1][1].body).debug).toBe(false);
    await screen.findByText('继续');
    expect(screen.queryByText(/链路 ·/)).not.toBeInTheDocument();
  });

  it.each(['methods', 'accessors'])('keeps the trace switch usable when storage %s throw', async mode => {
    window.history.replaceState({}, '', '/?debug=1');
    if (mode === 'methods') {
      vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota'); });
    } else {
      vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('SecurityError'); });
      vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => { throw new Error('SecurityError'); });
    }
    const fetchMock = vi.fn().mockResolvedValue(json(result('划痕', true)));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    const toggle = screen.getByRole('button', { name: '查看链路' });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    await userEvent.setup().click(toggle);
    await ask('划痕');
    await screen.findByText('链路 · 6 个阶段');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).debug).toBe(true);
    await userEvent.setup().click(toggle);
    expect(screen.queryByText(/链路 ·/)).not.toBeInTheDocument();
  });

  it('hides the mobile welcome on the first send and restores it for a new empty conversation', async () => {
    mockMobile();
    let resolve!: (value: Response) => void;
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => new Promise<Response>(done => { resolve = done; })));
    render(<App />);
    const heading = screen.getByRole('heading', { name: '你好，我是灵码的智能客服' });
    expect(screen.getByText('售后、配送、发票、商品参数，都可以问我。')).toBeInTheDocument();
    expect(screen.getByRole('log')).not.toContainElement(heading);
    await ask('火星');
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.queryByRole('heading', { name: '你好，我是灵码的智能客服' })).not.toBeInTheDocument();
    expect(screen.queryByText('售后、配送、发票、商品参数，都可以问我。')).not.toBeInTheDocument();
    await act(async () => { resolve(json(result('火星'))); });
    await screen.findByText('未找到答案');
    expect(screen.queryByRole('heading', { name: '你好，我是灵码的智能客服' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^常见问题$/ })).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(screen.getByRole('button', { name: '新对话' }));
    expect(screen.getByRole('heading', { name: '你好，我是灵码的智能客服' })).toBeInTheDocument();
    expect(screen.getByText('售后、配送、发票、商品参数，都可以问我。')).toBeInTheDocument();
  });

  it.each([639, 640, 641, 1024])('keeps the welcome limited to empty conversations at %ipx', async width => {
    mockViewportWidth(width);
    const response = result('火星');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(response)));
    const view = render(<App />);
    expect(screen.getByRole('heading', { name: '你好，我是灵码的智能客服' })).toBeInTheDocument();
    expect(screen.queryByText('样例数据')).not.toBeInTheDocument();
    expect(screen.queryByText('回答由 AI 根据店铺资料生成，仅供参考')).not.toBeInTheDocument();
    await ask('火星');
    await screen.findByText('未找到答案');
    expect(screen.queryByRole('heading', { name: '你好，我是灵码的智能客服' })).not.toBeInTheDocument();
    view.unmount();
    render(<App />);
    expect(screen.getByText(response.answer)).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '你好，我是灵码的智能客服' })).not.toBeInTheDocument();
  });

  it('renders offline answers with business wording and no mock label', async () => {
    const response = fixtureFor({ session_id: 's', message: '划痕', debug: false });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(response)));
    render(<App />);
    await ask('划痕');
    await screen.findByText('知识库回答');
    expect(screen.getByText('具体售后处理请以店铺核实结果为准。')).toBeInTheDocument();
    expect(screen.queryByText(/Mock ·|虚构|样例/)).not.toBeInTheDocument();
    expect(within(screen.getByLabelText('引用来源')).getAllByRole('article')).toHaveLength(3);
  });

  it('shows the cache fixture with the 秒回 badge', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(result('秒回'))));
    render(<App />);
    await ask('秒回');
    // 侧边栏的会话标题也可能叫「秒回」，只在消息区内断言路由标签。
    await waitFor(() => expect(within(screen.getByRole('log')).getByText('秒回', { selector: 'span' })).toBeInTheDocument());
  });

  it('hides server-supplied trace while the visible switch is off', async () => {
    vi.stubEnv('VITE_SHOW_TRACE_TOGGLE', '1');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(result('划痕', true))));
    render(<App />);
    await ask('划痕');
    await screen.findByText('知识库回答');
    expect(screen.getByRole('button', { name: '查看链路' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByText(/链路 ·/)).not.toBeInTheDocument();
  });

});
