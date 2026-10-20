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
    .process('@tailwind utilities;', { from: undefined })).css;
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

describe('shop entry at the CSS/JS mobile boundary', () => {
  it.each([639, 640, 641])('keeps exactly one visible entry at %ipx and through resizes', async initial => {
    const resize = mockViewportWidth(initial);
    sessionStorage.setItem(GUEST_ENTRY_KEY, '1'); accountMock();
    const style = document.createElement('style');
    document.head.append(style);
    try {
      style.textContent = responsiveCSS();
      await act(async () => { render(<App />); });
      for (const width of [initial, 639, 640, 641, 640, 639]) {
        act(() => { resize(width); style.textContent = responsiveCSS(); });
        const entries = screen.getAllByRole('button', { name: '店铺信息', hidden: true });
        expect(entries).toHaveLength(1);
        expect(entries[0]).toBeVisible();
        expect(screen.getAllByRole('button', { name: '店铺信息' })).toHaveLength(1);
        expect(Boolean(entries[0].closest('.composer-actions'))).toBe(width < 640);
        fireEvent.click(entries[0]);
        expect(screen.getAllByRole('dialog', { name: '店铺信息' })).toHaveLength(1);
        fireEvent.click(screen.getByRole('button', { name: '关闭店铺信息' }));
      }
      expect(window.matchMedia).toHaveBeenCalledWith('(max-width: 639px)');
      expect(window.matchMedia).toHaveBeenCalledWith('(min-width: 640px)');
    } finally { style.remove(); }
  });
});
