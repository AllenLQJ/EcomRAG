import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import theme from '../tailwind.config';

describe('browser branding', () => {
  it.each(['index.html', 'public/brand/index.html'])('uses the versioned SVG and ink theme color in %s', path => {
    const html = readFileSync(resolve(process.cwd(), path), 'utf8');
    const page = new DOMParser().parseFromString(html, 'text/html');
    const icon = page.querySelector('link[rel="icon"]');
    expect(icon?.getAttribute('href')).toBe('/favicon.svg?v=2');
    expect(icon?.getAttribute('type')).toBe('image/svg+xml');
    expect(page.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe(theme.theme.extend.colors.ink);
  });

  it('keeps the favicon identical to the ink seal, including the light glyph and vermilion dot', () => {
    const favicon = readFileSync(resolve(process.cwd(), 'public/favicon.svg'), 'utf8');
    const seal = readFileSync(resolve(process.cwd(), '../docs/brand/lingma-b-seal.svg'), 'utf8');
    expect(favicon).toBe(seal);
    const svg = new DOMParser().parseFromString(favicon, 'image/svg+xml');
    expect(svg.querySelector('title')?.textContent).toBe('灵码 · 墨印');
    expect(svg.querySelector('rect')?.getAttribute('fill')).toBe(theme.theme.extend.colors.ink);
    expect(Number(svg.querySelector('rect')?.getAttribute('rx'))).toBeGreaterThan(0);
    expect(svg.querySelector('path')?.getAttribute('stroke')).toBe(theme.theme.extend.colors.canvas);
    expect(svg.querySelector('circle')?.getAttribute('fill')).toBe('#E07A3F');
  });
});
