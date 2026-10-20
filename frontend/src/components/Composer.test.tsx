import { useState } from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { mockMobile } from '../test/media';
import { Composer } from './Composer';

function mount(initial = '', pending = false) {
  const onSend = vi.fn();
  function Harness() {
    const [value, setValue] = useState(initial);
    return <Composer value={value} onChange={setValue} onSend={onSend} pending={pending} />;
  }
  render(<Harness />);
  return { onSend, input: screen.getByRole('textbox', { name: '输入问题' }), send: screen.getByRole('button', { name: '发送' }) };
}

describe('Composer', () => {
  it.each([false, true])('shows only the input count below the composer (mobile=%s)', mobile => {
    mockMobile(mobile);
    const view = render(<Composer value="" onChange={vi.fn()} onSend={vi.fn()} pending={false} />);
    const input = screen.getByRole('textbox', { name: '输入问题' });
    expect(screen.queryByText('回答由 AI 根据店铺资料生成，仅供参考')).not.toBeInTheDocument();
    expect(screen.queryByText(/样例数据/)).not.toBeInTheDocument();
    expect(screen.queryByText('Enter 发送 · Shift + Enter 换行')).not.toBeInTheDocument();
    expect(screen.queryByText('对话内容会被记录，用于改进服务')).not.toBeInTheDocument();
    const countId = input.getAttribute('aria-describedby')!;
    expect(document.getElementById(countId)).toBeEmptyDOMElement();
    view.rerender(<Composer value="问题" onChange={vi.fn()} onSend={vi.fn()} pending={false} />);
    expect(document.getElementById(countId)).toHaveTextContent('2 / 500');
    expect(input).toHaveAccessibleDescription('2 / 500');
  });

  it('Enter sends; Shift+Enter inserts a newline', async () => {
    const user = userEvent.setup();
    const { input, onSend } = mount();
    await user.type(input, '问题{Shift>}{Enter}{/Shift}补充');
    expect(input).toHaveValue('问题\n补充');
    expect(onSend).not.toHaveBeenCalled();
    await user.keyboard('{Enter}');
    expect(onSend).toHaveBeenCalledWith('问题\n补充');
  });

  it('ignores Enter while composing, including native isComposing and keyCode 229', () => {
    const { input, onSend } = mount('中文');
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(input, { key: 'Enter', keyCode: 229 });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it('disables blank input', async () => {
    const user = userEvent.setup();
    const { input, send, onSend } = mount();
    expect(send).toBeDisabled();
    await user.type(input, '   {Enter}');
    expect(send).toBeDisabled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it('allows 500 Unicode characters; shows and blocks overflow without truncating input', () => {
    const { input, send, onSend } = mount();
    fireEvent.change(input, { target: { value: '字'.repeat(500) } });
    expect(send).toBeEnabled();
    expect(screen.getByText('500 / 500')).toBeInTheDocument();
    fireEvent.change(input, { target: { value: '字'.repeat(501) } });
    expect(send).toBeDisabled();
    expect(screen.getByText(/501 \/ 500/)).toBeInTheDocument();
    expect(input).toHaveAttribute('aria-invalid', 'true');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: '😀'.repeat(500) } });
    expect(send).toBeEnabled();
  });

  it('disables input, send and all quick questions during a request', () => {
    const { input } = mount('问题', true);
    expect(input).toBeDisabled();
    for (const button of screen.getAllByRole('button')) expect(button).toBeDisabled();
  });
  it('collapses mobile quick questions, exposes controls, sends once and collapses again', async () => {
    mockMobile();
    const user = userEvent.setup();
    const { input, onSend } = mount();
    expect(input).toHaveAttribute('placeholder', '输入你的问题');
    const toggle = screen.getByRole('button', { name: '常见问题' });
    const group = screen.getByLabelText('快捷问题');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', group.id);
    expect(group).not.toBeVisible();
    expect(within(group).queryAllByRole('button')).toHaveLength(0);
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(within(group).getAllByRole('button')).toHaveLength(4);
    await user.click(toggle);
    expect(group).not.toBeVisible();
    await user.click(toggle);
    await user.click(screen.getByRole('button', { name: '运费怎么算' }));
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith('运费怎么算');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(group).not.toBeVisible();
  });
  it('keeps all desktop quick questions and the original placeholder across breakpoint changes', () => {
    const setMobile = mockMobile(false);
    const { input } = mount();
    expect(input).toHaveAttribute('placeholder', '输入你的问题，例如：签收后多久可以退货？');
    expect(screen.queryByRole('button', { name: '常见问题' })).not.toBeInTheDocument();
    expect(within(screen.getByLabelText('快捷问题')).getAllByRole('button')).toHaveLength(4);
    act(() => setMobile(true));
    expect(input).toHaveAttribute('placeholder', '输入你的问题');
    expect(screen.getByRole('button', { name: '常见问题' })).toHaveAttribute('aria-expanded', 'false');
    act(() => setMobile(false));
    expect(within(screen.getByLabelText('快捷问题')).getAllByRole('button')).toHaveLength(4);
  });

  it('disables mobile quick questions and their disclosure while pending', () => {
    mockMobile();
    mount('问题', true);
    expect(screen.getByRole('button', { name: '常见问题' })).toBeDisabled();
  });

});
