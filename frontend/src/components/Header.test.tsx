import { createRef } from 'react';
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
    expect(header.children[0]).toHaveClass('min-w-0');
    expect(header.lastElementChild).toHaveClass('min-w-0', 'flex-wrap', 'justify-end');
    expect(shop).toHaveAttribute('aria-haspopup', 'dialog');
    expect(shop).toHaveAttribute('type', 'button');
    fireEvent.click(shop); expect(onShop).toHaveBeenCalledOnce();
  });
});
