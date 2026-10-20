import { createRef } from 'react';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Header } from './Header';

describe('header brand and actions', () => {
  it.each([true, false])('keeps the brand and wrapping actions without a central shop entry (sidebar=%s)', desktop => {
    render(<Header desktop={desktop} historyOpen={false} onHistory={vi.fn()} historyButton={createRef<HTMLButtonElement>()}
      name={'很长的店铺名称'.repeat(12)} supportUrl="https://support.example.test" onReset={vi.fn()}
      traceVisible traceEnabled={false} onToggleTrace={vi.fn()} />);
    const header = screen.getByRole('banner');
    expect(header).toHaveClass('flex', 'shrink-0', 'justify-between');
    expect(within(header).queryByRole('button', { name: '店铺商品' })).not.toBeInTheDocument();
    const brand = header.children[0];
    expect(brand).toHaveClass('min-w-0', 'overflow-hidden', 'items-center', 'sm:h-[var(--header-control-size)]');
    expect(brand.querySelector('img')).toHaveClass('h-8', 'w-8', 'shrink-0', 'sm:h-[var(--header-control-size)]', 'sm:w-[var(--header-control-size)]');
    expect(screen.getByText('客服中心')).toHaveClass('hidden', 'lg:inline', 'text-lg', 'text-muted', 'leading-none');
    expect(brand.querySelector('.font-serif')).toHaveClass('truncate', 'text-base', 'sm:text-2xl', 'sm:leading-none');
    expect(brand.querySelector('.items-baseline')).toHaveClass('min-w-0');
    expect(header.lastElementChild).toHaveClass('min-w-0', 'flex-wrap', 'justify-end');
  });
});
