import { createRef } from 'react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { Header } from './Header';
import { ShopInfoButton } from './ShopInfoButton';

describe('header shop action layout', () => {
  it.each([true, false])('reserves an equal-sided center column with wrapping side actions (sidebar=%s)', desktop => {
    const onShop = vi.fn();
    render(<Header desktop={desktop} historyOpen={false} onHistory={vi.fn()} historyButton={createRef<HTMLButtonElement>()}
      name={'很长的店铺名称'.repeat(12)} supportUrl="https://support.example.test" onReset={vi.fn()}
      traceVisible traceEnabled={false} onToggleTrace={vi.fn()} shopAction={<ShopInfoButton onClick={onShop} />} />);
    const header = screen.getByRole('banner');
    expect(header).toHaveClass('shrink-0', 'sm:grid', 'sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)]');
    const shop = screen.getByRole('button', { name: '店铺信息' });
    expect(shop.parentElement).toHaveClass('header-shop-action', 'hidden', 'sm:flex', 'justify-center');
    const brand = header.children[0];
    expect(brand).toHaveClass('min-w-0', 'overflow-hidden', 'items-center', 'sm:h-[var(--header-control-size)]');
    expect(brand.querySelector('img')).toHaveClass('h-8', 'w-8', 'shrink-0', 'sm:h-[var(--header-control-size)]', 'sm:w-[var(--header-control-size)]');
    expect(screen.getByText('客服中心')).toHaveClass('hidden', 'lg:inline', 'text-lg', 'text-muted', 'leading-none');
    expect(brand.querySelector('.font-serif')).toHaveClass('truncate', 'text-base', 'sm:text-2xl', 'sm:leading-none');
    expect(brand.querySelector('.items-baseline')).toHaveClass('min-w-0');
    expect(header.lastElementChild).toHaveClass('min-w-0', 'flex-wrap', 'justify-end');
    expect(shop).toHaveAttribute('aria-haspopup', 'dialog');
    expect(shop).toHaveAttribute('type', 'button');
    fireEvent.click(shop); expect(onShop).toHaveBeenCalledOnce();
  });
  it('shares the 44px header height with the shop action, including its mobile fallback', () => {
    const css = readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf8');
    expect(css).toMatch(/\.chat-header\s*\{\s*--header-control-size:\s*2\.75rem;/);
    expect(css).toMatch(/\.shop-info-button\s*\{[^}]*min-height:\s*var\(--header-control-size,\s*2\.75rem\)/);
  });
});
