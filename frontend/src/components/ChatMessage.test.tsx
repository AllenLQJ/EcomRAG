import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { turn } from '../test/conversations';
import { ChatMessage } from './ChatMessage';

describe('assistant elapsed badge', () => {
  it.each([6000, 0])('displays a known elapsed time, including a real zero (%s)', elapsedMs => {
    const message = turn('问题')[1];
    if (message.kind !== 'assistant') throw new Error('expected assistant');
    render(<ChatMessage message={{ ...message, elapsedMs }} pending={false} showTrace={false} onRetry={vi.fn()} onRephrase={vi.fn()} />);
    expect(screen.getByText(`${(elapsedMs / 1000).toFixed(1)} 秒`)).toBeInTheDocument();
  });

  it.each([undefined, null, Number.NaN, Number.POSITIVE_INFINITY, -1])('hides unknown or invalid elapsed time (%s)', elapsedMs => {
    const message = turn('问题')[1];
    if (message.kind !== 'assistant') throw new Error('expected assistant');
    render(<ChatMessage message={{ ...message, elapsedMs }} pending={false} showTrace={false} onRetry={vi.fn()} onRephrase={vi.fn()} />);
    expect(screen.getByText('闲聊')).toBeInTheDocument();
    expect(screen.queryByText(/秒$/)).not.toBeInTheDocument();
  });
});
