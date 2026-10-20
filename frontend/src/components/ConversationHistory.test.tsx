import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ConversationHistory } from './ConversationHistory';
import { conversation } from '../test/conversations';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

function setup() {
  const props = { conversations: [conversation('当前'), conversation('另一个')], currentId: '当前',
    onNew: vi.fn(), onSelect: vi.fn(), onDelete: vi.fn() };
  render(<ConversationHistory {...props} />);
  return props;
}

describe('conversation history navigation', () => {
  it('keeps rename reachable by keyboard and touch, hiding it only under hover-capable CSS', async () => {
    const rename = vi.fn().mockResolvedValue(true);
    render(<ConversationHistory conversations={[conversation('当前')]} currentId="当前"
      onNew={vi.fn()} onSelect={vi.fn()} onDelete={vi.fn()} onRename={rename} />);
    const button = screen.getByRole('button', { name: '重命名对话：当前' });
    expect(button).toHaveClass('history-rename', 'min-h-11', 'min-w-11');
    expect(button).not.toHaveAttribute('tabindex', '-1');
    expect(button).not.toHaveAttribute('aria-hidden');
    const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf8');
    const hoverOnly = css.match(/@media \(hover: hover\) \{([\s\S]*?)\n\}/)?.[1];
    expect(hoverOnly).toContain('.history-rename, .history-delete { opacity: 0; }');
    expect(hoverOnly).toContain('.group:hover .history-rename');
    expect(hoverOnly).toContain('.group:focus-within .history-rename');
    expect(button.className).not.toMatch(/hidden|opacity-0|invisible/);
    const deletion = screen.getByRole('button', { name: '删除对话：当前' });
    expect(deletion).toHaveClass('history-delete', 'min-h-11', 'min-w-11');
    expect(deletion.className).not.toMatch(/hidden|opacity-0|invisible|lg:/);
    expect(hoverOnly).toContain('.group:hover .history-delete');
    expect(hoverOnly).toContain('.group:focus-within .history-delete');
    expect(deletion).not.toHaveAttribute('tabindex', '-1');
    button.focus();
    await userEvent.setup().keyboard('{Enter}');
    expect(screen.getByLabelText('对话标题')).toHaveFocus();
    fireEvent.change(screen.getByLabelText('对话标题'), { target: { value: '新标题' } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '保存标题' })); });
    expect(rename).toHaveBeenCalledWith('当前', '新标题');
  });
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
    expect(screen.queryByText('历史仅保存在本浏览器')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '清空全部历史' })).not.toBeInTheDocument();
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

});

describe('confirmation identities and calendar refresh', () => {
  it('regroups at local midnight between the minute refreshes and clears its timers', () => {
    vi.useFakeTimers();
    const beforeMidnight = new Date(2026, 9, 18, 23, 59, 59, 500).getTime();
    vi.setSystemTime(beforeMidnight);
    const view = render(<ConversationHistory conversations={[conversation('昨天的提问', beforeMidnight - 300_000)]}
      currentId="none" onNew={vi.fn()} onSelect={vi.fn()} onDelete={vi.fn()} />);
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
