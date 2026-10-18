import { useState } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Composer } from './Composer';

function mount(initial = '', pending = false) {
  const onSend = vi.fn();
  function Harness() {
    const [value, setValue] = useState(initial);
    return <Composer value={value} onChange={setValue} onSend={onSend} pending={pending} sample={false} />;
  }
  render(<Harness />);
  return { onSend, input: screen.getByRole('textbox', { name: '输入问题' }), send: screen.getByRole('button', { name: '发送' }) };
}

describe('Composer', () => {
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
});
