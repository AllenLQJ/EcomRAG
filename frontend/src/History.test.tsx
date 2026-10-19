import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { CONVERSATIONS_KEY, loadConversations, saveConversations, SESSION_KEY } from './lib/storage';
import { conversation } from './test/conversations';
import { mockMobile } from './test/media';
import { controlledSSE } from './test/streams';

function seed() {
  saveConversations([conversation('最新', 200), conversation('较早', 100)]);
  localStorage.setItem(SESSION_KEY, '最新');
}

describe('conversation history integration', () => {
  it('restores a selected conversation, scrolls to bottom and continues with its id/context', async () => {
    seed();
    const stream = controlledSSE();
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<App />);
    const log = screen.getByRole('log');
    Object.defineProperties(log, { scrollHeight: { configurable: true, value: 1200 }, clientHeight: { configurable: true, value: 400 } });
    log.scrollTop = 200;
    fireEvent.scroll(log);
    fireEvent.click(screen.getByRole('button', { name: '打开对话：较早' }));
    expect(within(log).getByText('回答：较早')).toBeInTheDocument();
    expect(within(log).queryByText('回答：最新')).not.toBeInTheDocument();
    expect(log.scrollTop).toBe(1200);
    expect(localStorage.getItem(SESSION_KEY)).toBe('较早');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '继续' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ session_id: '较早', message: '继续', history: [
      { role: 'user', content: '较早' }, { role: 'assistant', content: '回答：较早' },
    ] });
    view.unmount();
  });
  it('aborts an in-flight stream on switch and never saves or restores the unfinished turn', async () => {
    seed();
    const stream = controlledSSE();
    const fetchMock = vi.fn().mockResolvedValue(stream.response);
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '未完成' } });
    fireEvent.click(screen.getByRole('button', { name: '发送' }));
    await act(async () => { stream.event({ event: 'delta', data: { text: '部分回答' } }); });
    fireEvent.click(screen.getByRole('button', { name: '打开对话：较早' }));
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    expect(screen.getByRole('textbox')).toBeEnabled();
    expect(screen.queryByText('部分回答')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开对话：最新' }));
    expect(within(screen.getByRole('log')).getByText('回答：最新')).toBeInTheDocument();
    expect(JSON.stringify(loadConversations())).not.toMatch(/未完成|部分回答/);
  });
  it('does not create empty entries and only removes history after confirmation', () => {
    seed();
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '新对话' }));
    expect(loadConversations()).toHaveLength(2);
    expect(screen.queryByRole('button', { current: 'page' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开对话：最新' }));
    fireEvent.click(screen.getByRole('button', { name: '删除对话：最新' }));
    expect(loadConversations()).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    expect(localStorage.getItem(SESSION_KEY)).toBe('较早');
    expect(within(screen.getByRole('log')).getByText('回答：较早')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '清空全部历史' }));
    expect(loadConversations()).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '确认清空' }));
    expect(JSON.parse(localStorage.getItem(CONVERSATIONS_KEY)!)).toEqual([]);
    expect(screen.getByRole('heading', { name: /你好，我是/ })).toBeInTheDocument();
  });
  it('deleting the last current conversation enters a fresh empty session', () => {
    saveConversations([conversation('唯一')]);
    localStorage.setItem(SESSION_KEY, '唯一');
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '删除对话：唯一' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    expect(loadConversations()).toEqual([]);
    expect(localStorage.getItem(SESSION_KEY)).not.toBe('唯一');
    expect(screen.getByRole('heading', { name: /你好，我是/ })).toBeInTheDocument();
  });
});

describe('history drawer', () => {
  it.each([390, 640, 800, 1023])('opens below desktop at %ipx with modal focus/scroll management', async width => {
    vi.stubGlobal('matchMedia', vi.fn((media: string) => ({ matches: media === '(min-width: 1024px)' ? width >= 1024 : width <= 640,
      addEventListener: vi.fn(), removeEventListener: vi.fn() })));
    seed();
    const user = userEvent.setup();
    const view = render(<App />);
    const trigger = screen.getByRole('button', { name: '历史' });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    await user.click(trigger);
    const dialog = screen.getByRole('dialog', { name: '历史对话抽屉' });
    const close = within(dialog).getByRole('button', { name: '关闭历史' });
    expect(close).toHaveFocus();
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(document.body.style.overflow).toBe('hidden');
    await user.keyboard('{Shift>}{Tab}{/Shift}');
    expect(within(dialog).getByRole('button', { name: '清空全部历史' })).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(document.body.style.overflow).toBe('');
    await user.click(trigger);
    await user.click(screen.getByRole('button', { name: '关闭历史遮罩' }));
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    await user.click(screen.getByRole('button', { name: '关闭历史' }));
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    view.unmount();
    expect(document.body.style.overflow).toBe('');
  });
  it('selects a conversation from the drawer and restores trigger focus', async () => {
    mockMobile(); seed();
    render(<App />);
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: '历史' });
    await user.click(trigger);
    await user.click(screen.getByRole('button', { name: '打开对话：较早' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(within(screen.getByRole('log')).getByText('回答：较早')).toBeInTheDocument();
  });
  it('uses the fixed sidebar at 1024px and closes the drawer on resize', async () => {
    const resize = mockMobile(true);
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '历史' }));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    act(() => resize(false));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '历史' })).not.toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: '历史对话' })).toBeInTheDocument();
    expect(document.body.style.overflow).toBe('');
  });
});

describe('drawer accessibility during confirmation', () => {
  it('leaves the history trigger outside hidden/inert ancestors and only hides main content', () => {
    mockMobile(); seed();
    render(<App />);
    const trigger = screen.getByRole('button', { name: '历史' });
    fireEvent.click(trigger);
    expect(screen.getByRole('button', { name: '历史' })).toBe(trigger);
    expect(trigger.closest('[aria-hidden="true"], [inert]')).toBeNull();
    expect(screen.getByRole('banner')).not.toHaveAttribute('aria-hidden');
    const main = screen.getByRole('main', { hidden: true });
    expect(main).toHaveAttribute('aria-hidden', 'true');
    expect(main).toHaveAttribute('inert');
    fireEvent.click(screen.getByRole('button', { name: '关闭历史' }));
    expect(main).not.toHaveAttribute('aria-hidden');
    expect(main).not.toHaveAttribute('inert');
    expect(trigger).toHaveFocus();
  });
  it.each(['delete', 'clear'])('uses the first Escape to cancel %s and the second to close the drawer', async type => {
    mockMobile(); seed();
    render(<App />);
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', { name: '历史' });
    await user.click(trigger);
    const nav = screen.getByRole('navigation', { name: '历史对话' });
    await user.click(within(nav).getByRole('button', { name: type === 'delete' ? '删除对话：最新' : '清空全部历史' }));
    expect(screen.getByRole('group', { name: type === 'delete' ? '确认删除：最新' : '确认清空全部历史' })).toBeInTheDocument();
    // Escape must cancel even with focus outside the navigation subtree.
    screen.getByRole('button', { name: '关闭历史' }).focus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('group')).not.toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(nav).getByRole('button', { name: '新对话' })).toHaveFocus();
    expect(loadConversations()).toHaveLength(2);
    expect(document.body.style.overflow).toBe('hidden');
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(document.body.style.overflow).toBe('');
    expect(loadConversations()).toHaveLength(2);
  });
});
