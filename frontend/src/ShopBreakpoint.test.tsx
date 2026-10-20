import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import postcss from 'postcss';
import tailwindcss from 'tailwindcss';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeAll, describe, expect, it } from 'vitest';
import App, { GUEST_ENTRY_KEY } from './App';
import theme from '../tailwind.config';
import { accountMock } from './test/account';
import { mockViewportWidth } from './test/media';

let utilities: string;
beforeAll(async () => {
  const raw = ['Header', 'Composer', 'ShopInfoButton'].map(name =>
    readFileSync(resolve(process.cwd(), `src/components/${name}.tsx`), 'utf8')).join('\n');
  utilities = (await postcss([tailwindcss({ ...theme, content: [{ raw, extension: 'tsx' }] })])
    .process(readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf8'), { from: undefined })).css;
});

// jsdom does not evaluate media rules. Flatten the actual generated Tailwind CSS
// using the same viewport evaluator as the JS hook, so visibility checks include CSS.
function responsiveCSS() {
  const root = postcss.parse(utilities);
  root.walkAtRules('media', rule => {
    if (window.matchMedia(rule.params).matches) rule.replaceWith(...(rule.nodes || []));
    else rule.remove();
  });
  return root.toString();
}

describe('floating shop entry at every viewport width', () => {
  it.each([320, 639, 640, 641, 1024])('keeps exactly one accessible fixed entry at %ipx and through resizes', async initial => {
    const resize = mockViewportWidth(initial);
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); accountMock();
    const style = document.createElement('style');
    document.head.append(style);
    try {
      style.textContent = responsiveCSS();
      await act(async () => { render(<App />); });
      const original = screen.getByRole('button', { name: '店铺商品' });
      for (const width of [initial, 320, 640, 1024, 390]) {
        act(() => { resize(width); style.textContent = responsiveCSS(); });
        const entries = screen.getAllByRole('button', { name: '店铺商品', hidden: true });
        expect(entries).toHaveLength(1);
        expect(entries[0]).toBeVisible();
        expect(screen.getAllByRole('button', { name: '店铺商品' })).toHaveLength(1);
        expect(entries[0]).toBe(original);
        expect(entries[0]).toHaveClass('shop-products-button');
        expect(entries[0]).toHaveStyle({ position: 'fixed' });
        expect(entries[0].closest('header, footer, form, nav')).toBeNull();
        expect(getComputedStyle(entries[0]).fontFamily).toBe(
          getComputedStyle(screen.getByRole('banner').querySelector('.font-serif')!).fontFamily);
        if (width < 640) {
          const quick = screen.getByRole('button', { name: '常见问题' });
          if (quick.getAttribute('aria-expanded') !== 'true') fireEvent.click(quick);
          expect(screen.getByLabelText('快捷问题')).toBeVisible();
          expect(entries[0]).toBeVisible();
        }
        fireEvent.click(entries[0]);
        expect(screen.getAllByRole('dialog', { name: '店铺商品' })).toHaveLength(1);
        fireEvent.click(screen.getByRole('button', { name: '关闭店铺商品' }));
      }
      expect(window.matchMedia).toHaveBeenCalledWith('(max-width: 639px)');
      expect(window.matchMedia).toHaveBeenCalledWith('(min-width: 1024px)');
    } finally { style.remove(); }
  });

  it('builds scheme D with an unchanged logo-orange dot on hover and reserves space below composer controls', () => {
    const css = postcss.parse(utilities);
    const declarations = (selector: string) => {
      const values: Record<string, string> = {};
      css.walkRules(rule => {
        if (rule.selectors.includes(selector)) rule.walkDecls(decl => { values[decl.prop] = decl.value; });
      });
      return values;
    };
    const button = declarations('.shop-products-button');
    expect(button).toMatchObject({ position: 'fixed', 'min-height': '2.75rem', 'border-radius': '9999px',
      'border-width': '1.5px', 'font-size': '17px', 'letter-spacing': '3px', 'line-height': '1.25',
      'white-space': 'nowrap', 'z-index': '30' });
    expect(button['font-family']).toContain('Noto Serif SC');
    expect(button['background-color']).toMatch(/^rgb\(255 255 255\s*\//);
    expect(button['border-color']).toMatch(/^rgb\(22 23 26\s*\//);
    expect(button.color).toMatch(/^rgb\(22 23 26\s*\//);
    expect(button.right).toBe('max(24px, calc(env(safe-area-inset-right) + 16px))');
    expect(button.bottom).toBe('max(16px, env(safe-area-inset-bottom))');
    const dot = declarations('.shop-products-dot');
    expect(dot).toMatchObject({ width: '7px', height: '7px' });
    const orange = theme.theme.extend.colors.spark;
    const rgb = [1, 3, 5].map(start => parseInt(orange.slice(start, start + 2), 16)).join(' ');
    expect(dot['background-color']).toContain(`rgb(${rgb} /`);
    const logo = new DOMParser().parseFromString(readFileSync(resolve(process.cwd(), 'public/brand/lingma-b-seal.svg'), 'utf8'), 'image/svg+xml');
    expect(logo.querySelector('circle')?.getAttribute('fill')?.toLowerCase()).toBe(orange.toLowerCase());
    const hover = declarations('.shop-products-button:hover');
    expect(hover.transform).toBe('translateY(-2px)');
    expect(hover['background-color']).toMatch(/^rgb\(22 23 26\s*\//);
    expect(hover.color).toMatch(/^rgb\(255 255 255\s*\//);
    expect(hover.filter).toBeUndefined();
    expect(declarations('.shop-products-button:hover .shop-products-dot')['background-color']).toBeUndefined();
    const footer = declarations('.composer-footer');
    expect(footer['--shop-products-height']).toBe(button['min-height']);
    expect(footer['--shop-products-gap']).toBe('1rem');
    expect(footer['padding-bottom']).toBe('calc(max(16px, env(safe-area-inset-bottom)) + var(--shop-products-height) + var(--shop-products-gap))');
  });
});
