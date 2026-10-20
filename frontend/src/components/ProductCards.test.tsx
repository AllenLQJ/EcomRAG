import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { product, productAnswer } from '../test/products';
import { ChatMessage } from './ChatMessage';
import { ProductCards } from './ProductCards';

describe('product cards', () => {
  it.each([undefined, []])('adds no markup without products: %j', products => {
    const { container } = render(<ProductCards products={products} />);
    expect(container).toBeEmptyDOMElement();
  });

  it.each([1, 2, 3, 6, 12])('renders %i cards in server order with names, categories and up to two highlights', count => {
    const products = Array.from({ length: count }, (_, index) => product(`lamp-${index}`));
    render(<ProductCards products={products} />);
    const section = screen.getByRole('region', { name: '资料关联商品' });
    expect(within(section).getAllByRole('heading').map(heading => heading.textContent)).toEqual(products.map(item => item.name));
    expect(within(section).getAllByText('桌面台灯')).toHaveLength(count);
    expect(within(section).getAllByText(products[0].highlights[0])).toHaveLength(count);
    expect(within(section).getAllByText(products[0].highlights[1])).toHaveLength(count);
    expect(section.querySelector('a, button, [tabindex]')).toBeNull();
    expect(section).not.toHaveTextContent(/价格|库存/);
  });

  it('bounds cards by the response contract and highlights to two, and renders highlight HTML as plain text', () => {
    const products = Array.from({ length: 13 }, (_, index) => ({ ...product(`lamp-${index}`),
      highlights: ['<b>额定功率：6 瓦</b>', '连接方式：USB', '不应显示的第三条规格'], price: 199, stock: 10 }));
    const { container } = render(<ProductCards products={products} />);
    expect(screen.getAllByRole('heading')).toHaveLength(12);
    expect(screen.getAllByText('<b>额定功率：6 瓦</b>')).toHaveLength(12);
    expect(screen.queryByText('不应显示的第三条规格')).not.toBeInTheDocument();
    expect(container.querySelector('b')).toBeNull();
    expect(container).not.toHaveTextContent('199');
  });

  it('renders a single supplied highlight without inventing a second one', () => {
    render(<ProductCards products={[{ ...product(), highlights: ['额定功率：6 瓦'] }]} />);
    expect(screen.getByText('额定功率：6 瓦')).toBeInTheDocument();
    expect(screen.queryByText(product().highlights[1])).not.toBeInTheDocument();
  });

  it('uses a single column on narrow screens and semantic tokens for both color schemes', () => {
    render(<ProductCards products={[product()]} />);
    const grid = screen.getAllByRole('list')[0];
    expect(grid).toHaveClass('grid', 'grid-cols-1', 'sm:grid-cols-2', 'lg:grid-cols-3');
    expect(grid.firstElementChild).toHaveClass('bg-card', 'text-ink', 'dark:bg-ink', 'dark:text-card');
  });

  it('keeps six equal-size cards inside the chat width across multiple rows', () => {
    render(<ProductCards products={Array.from({ length: 6 }, (_, index) => product(`lamp-${index}`))} />);
    const section = screen.getByRole('region', { name: '资料关联商品' });
    expect(section).toHaveClass('min-w-0', 'max-w-full');
    const grid = within(section).getAllByRole('list')[0];
    expect(grid).toHaveClass('grid', 'auto-rows-fr', 'grid-cols-1', 'sm:grid-cols-2', 'lg:grid-cols-3');
    expect(grid.children).toHaveLength(6);
    Array.from(grid.children).forEach(card => {
      expect(card).toHaveClass('flex', 'h-full', 'min-w-0', 'flex-col', 'overflow-hidden');
      expect(card.firstElementChild).toHaveClass('aspect-[4/3]', 'w-full', 'shrink-0');
      expect(card).toHaveClass('bg-card', 'text-ink', 'dark:bg-ink', 'dark:text-card');
    });
  });

  it('reserves image space, supplies product alt text and lazy loading, then replaces failed images', () => {
    render(<ProductCards products={[product()]} />);
    const img = screen.getByRole('img', { name: product().name });
    expect(img).toHaveAttribute('loading', 'lazy');
    expect(img).toHaveAttribute('width', '400');
    expect(img).toHaveAttribute('height', '300');
    expect(img.parentElement).toHaveClass('aspect-[4/3]');
    fireEvent.error(img);
    expect(screen.queryByAltText(product().name)).not.toBeInTheDocument();
    const placeholder = screen.getByRole('img', { name: `${product().name}（暂无图片）` });
    expect(placeholder).toHaveTextContent('暂无图片');
    expect(placeholder.parentElement).toHaveClass('aspect-[4/3]');
    expect(placeholder.querySelector('img')).toBeNull();
  });

  it.each([null, ''])('uses the same built-in placeholder for a missing image: %j', image_url => {
    const { container } = render(<ProductCards products={[{ ...product(), image_url }]} />);
    expect(screen.getByRole('img', { name: `${product().name}（暂无图片）` })).toHaveTextContent('暂无图片');
    expect(container.querySelector('img')).toBeNull();
  });

  it('tries a replacement image URL for the same SKU after a previous image fails', () => {
    const { rerender } = render(<ProductCards products={[product()]} />);
    fireEvent.error(screen.getByAltText(product().name));
    rerender(<ProductCards products={[{ ...product(), image_url: 'https://other.example/main.webp' }]} />);
    expect(screen.getByAltText(product().name)).toHaveAttribute('src', 'https://other.example/main.webp');
  });

  it('keeps legacy assistant markup unchanged when products is absent and places cards below the answer', () => {
    const response = productAnswer();
    const props = { pending: false, showTrace: false, onRetry: vi.fn(), onRephrase: vi.fn() };
    const message = { id: 'a', kind: 'assistant' as const, question: '台灯规格', elapsedMs: 1, response };
    const { container, rerender } = render(<ChatMessage {...props} message={message} />);
    const legacy = container.innerHTML;
    expect(screen.queryByRole('region', { name: '资料关联商品' })).not.toBeInTheDocument();
    rerender(<ChatMessage {...props} message={{ ...message, response: { ...response, products: [] } }} />);
    expect(container.innerHTML).toBe(legacy);
    rerender(<ChatMessage {...props} message={{ ...message, response: productAnswer([product()]) }} />);
    const answer = screen.getByText(response.answer);
    const cards = screen.getByRole('region', { name: '资料关联商品' });
    expect(answer.compareDocumentPosition(cards) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
