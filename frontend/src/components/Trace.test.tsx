import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import postcss from 'postcss';
import tailwindcss from 'tailwindcss';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, expect, it } from 'vitest';
import theme from '../../tailwind.config';
import { product } from '../test/products';
import { ProductCards } from './ProductCards';
import { Sources } from './Sources';
import { Trace } from './Trace';

let disclosureCSS: string;
beforeAll(async () => {
  const raw = ['Trace', 'Sources', 'ProductCards'].map(name =>
    readFileSync(resolve(process.cwd(), `src/components/${name}.tsx`), 'utf8')).join('\n');
  disclosureCSS = (await postcss([tailwindcss({ ...theme, content: [{ raw, extension: 'tsx' }] })])
    .process(readFileSync(resolve(process.cwd(), 'src/index.css'), 'utf8'), { from: undefined })).css;
});

it('shows integer milliseconds, two-place scores and Chinese route/speculative labels', async () => {
  render(<Trace trace={[
    { stage: 'rerank', ms: 123.75, top_score: 0.605768371524513, route: 'rag', speculative: true },
    { stage: 'future', ms: 0.4, custom: '原样', speculative: false },
  ]} />);
  await userEvent.setup().click(screen.getByText('链路 · 2 个阶段'));
  expect(screen.getByText('124 ms')).toBeVisible();
  expect(screen.getByText('0 ms')).toBeVisible();
  expect(screen.getByText('最高分: 0.61 · 来源: 知识库回答 · 推测检索: 是')).toBeVisible();
  expect(screen.getByText('custom: 原样 · 推测检索: 否')).toBeVisible();
  expect(screen.queryByText(/0\.605768/)).not.toBeInTheDocument();
});

it('keeps trace, source and product disclosures visually consistent with triangle markers on the left', async () => {
  const user = userEvent.setup();
  // Inspect generated declarations: jsdom does not fully resolve Tailwind color variables.
  const css = postcss.parse(disclosureCSS);
  const declarations = (selector: string) => {
    const values: Record<string, string> = {};
    css.walkRules(rule => {
      if (rule.selectors.includes(selector)) rule.walkDecls(decl => { values[decl.prop] = decl.value; });
    });
    return values;
  };
  expect(declarations('.disclosure-panel')).toMatchObject({ 'border-width': '1px', 'border-radius': '12px' });
  expect(declarations('.disclosure-panel')['background-color']).toMatch(/^rgb\(255 255 255\s*\//);
  expect(declarations('.disclosure-trigger')).toMatchObject({ 'font-size': '13px', 'font-weight': '500',
    'padding-top': '0.75rem', 'padding-bottom': '0.75rem', 'min-height': '2.75rem', 'list-style': 'none' });
  expect(declarations('.disclosure-marker')['clip-path']).toBe('polygon(0 0, 100% 50%, 0 100%)');
  expect(declarations("[aria-expanded='true'] > .disclosure-marker").transform).toBe('rotate(90deg)');
  expect(declarations('details[open] > .disclosure-trigger > .disclosure-marker').transform).toBe('rotate(90deg)');
  expect(declarations('.disclosure-trigger::-webkit-details-marker').display).toBe('none');
  render(<>
    <Trace trace={[{ stage: 'generate', ms: 1 }]} />
    <Sources sources={[{ index: 1, doc_id: 'doc', title: '资料', snippet: '来源说明', score: 1 }]} selection={null} />
    <ProductCards products={[product()]} />
  </>);
  const trace = screen.getByText('链路 · 1 个阶段');
  const toggles = [trace, screen.getByRole('button', { name: '引用来源（1）' }),
    screen.getByRole('button', { name: product().name })];
  expect(trace.closest('details')).not.toHaveAttribute('open');
  for (const toggle of toggles) {
    expect(toggle).toHaveClass('disclosure-trigger');
    expect(toggle.closest('.disclosure-panel')).toBeInTheDocument();
    expect(toggle.firstElementChild).toHaveClass('disclosure-marker');
    expect(toggle.firstElementChild).toHaveAttribute('aria-hidden', 'true');
    // Product text is inset within the image card; sources and trace use the outer panel inset.
    const inset = toggle.tagName === 'SUMMARY' ? toggle.parentElement! : toggle.parentElement!.parentElement!;
    expect(inset).toHaveClass('px-4');
    if (toggle !== trace) expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await user.click(toggle);
    if (toggle === trace) expect(trace.closest('details')).toHaveAttribute('open');
    else expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await user.click(toggle);
  }
  expect(trace.closest('details')).not.toHaveAttribute('open');
  expect(toggles[1]).toHaveAttribute('aria-expanded', 'false');
  expect(toggles[2]).toHaveAttribute('aria-expanded', 'false');
  expect(screen.getByText('来源说明')).not.toBeVisible();
  expect(screen.getByText(product().highlights[0])).not.toBeVisible();
});
