import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import theme from '../../tailwind.config';
import { mockViewportWidth } from '../test/media';
import { product, productAnswer } from '../test/products';
import { ChatMessage } from './ChatMessage';
import { ProductCards } from './ProductCards';

function expectExpanded(buttons: HTMLElement[], indices: number[]) {
  buttons.forEach((button, index) => {
    const expanded = indices.includes(index);
    expect(button).toHaveAttribute('aria-expanded', String(expanded));
    const details = document.getElementById(button.getAttribute('aria-controls')!);
    if (expanded) expect(details).toBeVisible();
    else expect(details).not.toBeVisible();
  });
}

describe('product cards', () => {
  beforeEach(() => { mockViewportWidth(390); });

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

  it('uses a single column on narrow screens and the same disclosure style as trace', () => {
    const { container } = render(<ProductCards products={[product()]} />);
    const grid = screen.getAllByRole('list')[0];
    expect(grid).toHaveClass('grid', 'grid-cols-1', 'sm:grid-cols-2', 'lg:grid-cols-3');
    expect(grid.firstElementChild).toHaveClass('disclosure-panel');
    expect(container.querySelector('.product-card-info')).toHaveClass('px-4');
    const toggle = screen.getByRole('button', { name: product().name });
    expect(toggle).toHaveClass('disclosure-trigger');
    expect(toggle.firstElementChild).toHaveClass('disclosure-marker');
    expect(toggle.firstElementChild).toHaveAttribute('aria-hidden', 'true');
    expect(toggle.querySelector('svg')).toBeNull();
  });

  it('keeps six square-image cards equal in height within each row', () => {
    render(<ProductCards products={Array.from({ length: 6 }, (_, index) => product(`lamp-${index}`))} />);
    const section = screen.getByRole('region', { name: '资料关联商品' });
    expect(section).toHaveClass('min-w-0', 'max-w-full');
    const grid = within(section).getAllByRole('list')[0];
    expect(grid).toHaveClass('grid', 'items-stretch', 'grid-cols-1', 'sm:grid-cols-2', 'lg:grid-cols-3');
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

  it('keeps separate mobile rows and separate grids independent with unique accessible controls', async () => {
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

  it.each([[390, 1], [639, 1], [640, 2], [641, 2], [1023, 2], [1024, 3], [1440, 3]])(
    'shares expansion and collapse only within each %i px row (%i columns), including the final partial row',
    async (width, columns) => {
      mockViewportWidth(width);
      const user = userEvent.setup();
      render(<ProductCards products={Array.from({ length: 7 }, (_, index) => product(`lamp-${index}`))} />);
      const buttons = screen.getAllByRole('button');
      const first = Array.from({ length: columns }, (_, index) => index);
      const second = first.map(index => index + columns);
      expectExpanded(buttons, []);
      await user.click(buttons[columns - 1]);
      expectExpanded(buttons, first);
      await user.click(buttons[columns]);
      expectExpanded(buttons, [...first, ...second]);
      await user.click(buttons[0]);
      expectExpanded(buttons, second);
      await user.click(buttons[6]);
      expectExpanded(buttons, [...second, 6]);
      await user.click(buttons[6]);
      expectExpanded(buttons, second);
    },
  );

  it('regroups and collapses on column-count changes while preserving state within the same breakpoint', async () => {
    const resize = mockViewportWidth(1280);
    const user = userEvent.setup();
    render(<ProductCards products={Array.from({ length: 7 }, (_, index) => product(`lamp-${index}`))} />);
    const buttons = screen.getAllByRole('button');
    await user.click(buttons[1]);
    expectExpanded(buttons, [0, 1, 2]);
    act(() => resize(900));
    expectExpanded(buttons, []);
    await user.click(buttons[1]);
    expectExpanded(buttons, [0, 1]);
    act(() => resize(800));
    expectExpanded(buttons, [0, 1]);
    act(() => resize(639));
    expectExpanded(buttons, []);
    await user.click(buttons[1]);
    expectExpanded(buttons, [1]);
    act(() => resize(640));
    expectExpanded(buttons, []);
    await user.click(buttons[2]);
    expectExpanded(buttons, [2, 3]);
    act(() => resize(1024));
    expectExpanded(buttons, []);
    await user.click(buttons[5]);
    expectExpanded(buttons, [3, 4, 5]);
    act(() => resize(1300));
    expectExpanded(buttons, [3, 4, 5]);
    expect(screen.getAllByRole('button')).toEqual(buttons);
  });

  it('does not reuse expanded rows for different products', async () => {
    mockViewportWidth(1280);
    const user = userEvent.setup();
    const products = Array.from({ length: 6 }, (_, index) => product(`lamp-${index}`));
    const { rerender } = render(<ProductCards products={products} />);
    await user.click(screen.getAllByRole('button')[1]);
    expectExpanded(screen.getAllByRole('button'), [0, 1, 2]);
    rerender(<ProductCards products={products.slice(1)} />);
    expectExpanded(screen.getAllByRole('button'), []);
  });

  it('uses the same breakpoints as the CSS grid and removes change listeners on unmount', () => {
    expect(theme.theme.screens).toMatchObject({ sm: '640px', lg: '1024px' });
    const queries: { addEventListener: ReturnType<typeof vi.fn>; removeEventListener: ReturnType<typeof vi.fn> }[] = [];
    vi.stubGlobal('matchMedia', vi.fn((media: string) => {
      const query = { media, matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() };
      queries.push(query);
      return query;
    }));
    const { unmount } = render(<ProductCards products={[product()]} />);
    const subscribed = queries.filter(query => query.addEventListener.mock.calls.length);
    expect(subscribed).toHaveLength(2);
    expect(window.matchMedia).toHaveBeenCalledWith('(min-width: 640px)');
    expect(window.matchMedia).toHaveBeenCalledWith('(min-width: 1024px)');
    unmount();
    subscribed.forEach(query => expect(query.removeEventListener).toHaveBeenCalledWith(
      'change', query.addEventListener.mock.calls[0][1]));
  });

  it.each(['{Enter}', ' '])('supports native keyboard activation with %j', async key => {
    mockViewportWidth(1280);
    const user = userEvent.setup();
    render(<ProductCards products={Array.from({ length: 4 }, (_, index) => product(`lamp-${index}`))} />);
    const buttons = screen.getAllByRole('button');
    const button = buttons[0];
    await user.tab();
    expect(button).toHaveFocus();
    await user.keyboard(key);
    expectExpanded(buttons, [0, 1, 2]);
    await user.keyboard(key);
    expectExpanded(buttons, []);
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
