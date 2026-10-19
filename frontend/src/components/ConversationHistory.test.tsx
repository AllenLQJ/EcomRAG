import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ConversationHistory } from './ConversationHistory';
import { conversation } from '../test/conversations';

function setup() {
  const props = { conversations: [conversation('当前'), conversation('另一个')], currentId: '当前',
    onNew: vi.fn(), onSelect: vi.fn(), onDelete: vi.fn(), onClear: vi.fn() };
  render(<ConversationHistory {...props} />);
  return props;
}

describe('conversation history navigation', () => {
  it('provides navigation/list semantics, current state and keyboard selection', async () => {
    const props = setup();
    expect(screen.getByRole('navigation', { name: '历史对话' })).toBeInTheDocument();
    expect(screen.getByRole('list', { name: '今天的对话' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '打开对话：当前' })).toHaveAttribute('aria-current', 'page');
    const next = screen.getByRole('button', { name: '打开对话：另一个' });
    expect(next).not.toHaveAttribute('aria-current');
    next.focus();
    await userEvent.setup().keyboard('{Enter}');
    expect(props.onSelect).toHaveBeenCalledWith('另一个');
    expect(screen.getByText('历史仅保存在本浏览器')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '新对话' }));
    expect(props.onNew).toHaveBeenCalledOnce();
  });
  it('requires explicit confirmation for deletion, supports cancelling and restores focus', async () => {
    const props = setup();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '删除对话：当前' }));
    expect(props.onDelete).not.toHaveBeenCalled();
    const confirm = screen.getByRole('group', { name: '确认删除：当前' });
    await user.click(within(confirm).getByRole('button', { name: '取消' }));
    expect(props.onDelete).not.toHaveBeenCalled();
    expect(screen.queryByRole('group')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '删除对话：另一个' }));
    await user.click(screen.getByRole('button', { name: '确认删除' }));
    expect(props.onDelete).toHaveBeenCalledTimes(1);
    expect(props.onDelete).toHaveBeenCalledWith('另一个');
    expect(screen.getByRole('button', { name: '新对话' })).toHaveFocus();
  });
  it('requires a second action to clear all history', async () => {
    const props = setup();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '清空全部历史' }));
    expect(props.onClear).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(props.onClear).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '清空全部历史' }));
    await user.click(screen.getByRole('button', { name: '确认清空' }));
    expect(props.onClear).toHaveBeenCalledOnce();
  });
});

describe('confirmation identities and calendar refresh', () => {
  it('keeps a tampered clear-all ID separate from the clear confirmation', () => {
    const props = { conversations: [{ ...conversation('clear-all'), title: '篡改标题' }], currentId: 'clear-all',
      onNew: vi.fn(), onSelect: vi.fn(), onDelete: vi.fn(), onClear: vi.fn() };
    render(<ConversationHistory {...props} />);
    fireEvent.click(screen.getByRole('button', { name: '删除对话：篡改标题' }));
    expect(screen.getAllByRole('group')).toHaveLength(1);
    expect(screen.getByRole('group', { name: '确认删除：篡改标题' })).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: '确认清空全部历史' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    expect(props.onDelete).toHaveBeenCalledWith('clear-all');
    expect(props.onClear).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '清空全部历史' }));
    expect(screen.getAllByRole('group')).toHaveLength(1);
    expect(screen.queryByRole('group', { name: '确认删除：篡改标题' })).not.toBeInTheDocument();
    expect(screen.getByRole('group', { name: '确认清空全部历史' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认清空' }));
    expect(props.onClear).toHaveBeenCalledOnce();
    expect(props.onDelete).toHaveBeenCalledTimes(1);
  });
  it('regroups at local midnight between the minute refreshes and clears its timers', () => {
    vi.useFakeTimers();
    const beforeMidnight = new Date(2026, 9, 18, 23, 59, 59, 500).getTime();
    vi.setSystemTime(beforeMidnight);
    const view = render(<ConversationHistory conversations={[conversation('昨天的提问', beforeMidnight - 300_000)]}
      currentId="none" onNew={vi.fn()} onSelect={vi.fn()} onDelete={vi.fn()} onClear={vi.fn()} />);
    expect(screen.getByRole('list', { name: '今天的对话' })).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(499));
    expect(screen.getByRole('list', { name: '今天的对话' })).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByRole('list', { name: '今天的对话' })).not.toBeInTheDocument();
    expect(screen.getByRole('list', { name: '昨天的对话' })).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(2);
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
