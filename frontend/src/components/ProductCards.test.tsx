import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { product, productAnswer } from '../test/products';
import { ChatMessage } from './ChatMessage';
import { ProductCards } from './ProductCards';

describe('product cards', () => {
  it.each([undefined, []])('adds no markup without products: %j', products => {
    const { container } = render(<ProductCards products={products} />);
    expect(container).toBeEmptyDOMElement();
  });

  it.each([1, 2, 3, 6, 12])('renders %i collapsed cards in server order with only names visible', count => {
    const products = Array.from({ length: count }, (_, index) => product(`lamp-${index}`));
    render(<ProductCards products={products} />);
    const section = screen.getByRole('region', { name: '资料关联商品' });
    expect(within(section).getAllByRole('heading').map(heading => heading.textContent)).toEqual(products.map(item => item.name));
    within(section).getAllByRole('button').forEach(button => expect(button).toHaveAttribute('aria-expanded', 'false'));
    for (const text of ['桌面台灯', ...products[0].highlights]) {
      const details = within(section).getAllByText(text);
      expect(details).toHaveLength(count);
      details.forEach(detail => expect(detail).not.toBeVisible());
    }
    expect(section.querySelector('a')).toBeNull();
    expect(section).not.toHaveTextContent(/价格|库存/);
  });

  it('bounds cards by the response contract and highlights to two, and renders highlight HTML as plain text', () => {
    const products = Array.from({ length: 13 }, (_, index) => ({ ...product(`lamp-${index}`),
      highlights: ['<b>额定功率：6 瓦</b>', '连接方式：USB', '不应显示的第三条规格'], price: 199, stock: 10 }));
    const { container } = render(<ProductCards products={products} />);
    screen.getAllByRole('button').forEach(button => fireEvent.click(button));
    expect(screen.getAllByRole('heading')).toHaveLength(12);
    expect(screen.getAllByText('<b>额定功率：6 瓦</b>')).toHaveLength(12);
    expect(screen.queryByText('不应显示的第三条规格')).not.toBeInTheDocument();
    expect(container.querySelector('b')).toBeNull();
    expect(container).not.toHaveTextContent('199');
  });

  it('renders a single supplied highlight without inventing a second one', () => {
    render(<ProductCards products={[{ ...product(), highlights: ['额定功率：6 瓦'] }]} />);
    fireEvent.click(screen.getByRole('button', { name: product().name }));
    expect(screen.getByText('额定功率：6 瓦')).toBeVisible();
    expect(screen.queryByText(product().highlights[1])).not.toBeInTheDocument();
  });

  it('uses a single column on narrow screens and a light information panel in both color schemes', () => {
    const { container } = render(<ProductCards products={[product()]} />);
    const grid = screen.getAllByRole('list')[0];
    expect(grid).toHaveClass('grid', 'grid-cols-1', 'sm:grid-cols-2', 'lg:grid-cols-3');
    expect(container.querySelector('.product-card-info')).toBeInTheDocument();
  });

  it('keeps six equal-width square-image cards aligned at the top without stretching expanded peers', () => {
    render(<ProductCards products={Array.from({ length: 6 }, (_, index) => product(`lamp-${index}`))} />);
    const section = screen.getByRole('region', { name: '资料关联商品' });
    expect(section).toHaveClass('min-w-0', 'max-w-full');
    const grid = within(section).getAllByRole('list')[0];
    expect(grid).toHaveClass('grid', 'items-start', 'grid-cols-1', 'sm:grid-cols-2', 'lg:grid-cols-3');
    expect(grid).not.toHaveClass('auto-rows-fr');
    expect(grid.children).toHaveLength(6);
    Array.from(grid.children).forEach(card => {
      expect(card).toHaveClass('flex', 'min-w-0', 'flex-col', 'overflow-hidden');
      expect(card).not.toHaveClass('h-full');
      expect(card.firstElementChild).toHaveClass('product-card-image', 'aspect-square', 'w-full', 'shrink-0');
      expect(card.lastElementChild).toHaveClass('product-card-info');
    });
  });

  it('reserves image space, supplies product alt text and lazy loading, then replaces failed images', () => {
    render(<ProductCards products={[product()]} />);
    const img = screen.getByRole('img', { name: product().name });
    expect(img).toHaveAttribute('loading', 'lazy');
    expect(img).toHaveAttribute('width', '400');
    expect(img).toHaveAttribute('height', '400');
    expect(img).toHaveClass('object-cover', 'h-full', 'w-full');
    expect(img.parentElement).toHaveClass('aspect-square');
    fireEvent.error(img);
    expect(screen.queryByAltText(product().name)).not.toBeInTheDocument();
    const placeholder = screen.getByRole('img', { name: `${product().name}（暂无图片）` });
    expect(placeholder).toHaveTextContent('暂无图片');
    expect(placeholder.parentElement).toHaveClass('aspect-square');
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

  it('toggles category and highlights independently with unique accessible controls', async () => {
    const user = userEvent.setup();
    const products = [product('lamp-1'), product('lamp-2')];
    render(<><ProductCards products={products} /><ProductCards products={[product('lamp-1')]} /></>);
    const buttons = screen.getAllByRole('button');
    const ids = buttons.map(button => button.getAttribute('aria-controls'));
    expect(new Set(ids).size).toBe(3);
    const panel = document.getElementById(ids[0]!);
    expect(panel).not.toBeVisible();
    expect(within(buttons[0]).getByText(products[0].name)).toHaveClass('truncate');
    await user.click(buttons[0]);
    expect(buttons[0]).toHaveAttribute('aria-expanded', 'true');
    expect(panel).toBeVisible();
    expect(within(panel!).getByText('桌面台灯')).toBeVisible();
    products[0].highlights.forEach(text => expect(within(panel!).getByText(text)).toBeVisible());
    buttons.slice(1).forEach(button => expect(button).toHaveAttribute('aria-expanded', 'false'));
    await user.click(buttons[0]);
    expect(buttons[0]).toHaveAttribute('aria-expanded', 'false');
    expect(panel).not.toBeVisible();
  });

  it.each(['{Enter}', ' '])('supports native keyboard activation with %j', async key => {
    const user = userEvent.setup();
    render(<ProductCards products={[product()]} />);
    const button = screen.getByRole('button', { name: product().name });
    await user.tab();
    expect(button).toHaveFocus();
    await user.keyboard(key);
    expect(button).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('桌面台灯')).toBeVisible();
    await user.keyboard(key);
    expect(button).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('桌面台灯')).not.toBeVisible();
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
