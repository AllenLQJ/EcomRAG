import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Source } from '../api/types';
import { Sources } from './Sources';

const sources: Source[] = [1, 2].map(index => ({ index, doc_id: `doc-${index}`,
  title: `资料 ${index}`, snippet: `摘要 ${index}`, score: .9 }));

describe('source disclosure', () => {
  it('shows only the counted title by default with an accessible hidden panel', () => {
    render(<Sources sources={sources} selection={null} />);
    const section = screen.getByRole('region', { name: '引用来源' });
    const toggle = within(section).getByRole('button', { name: '引用来源（2）' });
    expect(section).toHaveClass('sources-panel');
    expect(toggle).toHaveClass('sources-toggle', 'min-h-11', 'w-full', 'text-left');
    expect(toggle).toHaveAttribute('type', 'button');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(document.getElementById(toggle.getAttribute('aria-controls')!)).toHaveAttribute('hidden');
    expect(within(section).queryByRole('article')).not.toBeInTheDocument();
    sources.forEach(source => {
      expect(within(section).getByText(source.title)).not.toBeVisible();
      expect(within(section).getByText(source.snippet)).not.toBeVisible();
    });
  });

  it('opens and closes by clicking the title row', async () => {
    const user = userEvent.setup();
    render(<Sources sources={sources} selection={null} />);
    const toggle = screen.getByRole('button', { name: '引用来源（2）' });
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(screen.getByText('摘要 1')).toBeVisible();
    expect(toggle.querySelector('svg')).toHaveClass('rotate-180');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('摘要 1')).not.toBeVisible();
    expect(toggle.querySelector('svg')).not.toHaveClass('rotate-180');
  });

  it.each(['{Enter}', ' '])('supports Tab focus and native activation with %j', async key => {
    const user = userEvent.setup();
    render(<Sources sources={sources} selection={null} />);
    const toggle = screen.getByRole('button', { name: '引用来源（2）' });
    await user.tab();
    expect(toggle).toHaveFocus();
    await user.keyboard(key);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('摘要 1')).toBeVisible();
    await user.keyboard(key);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
  });

  it('keeps separate answers independent and gives each panel a unique ID', async () => {
    const user = userEvent.setup();
    render(<><Sources sources={sources} selection={null} /><Sources sources={sources} selection={null} /></>);
    const toggles = screen.getAllByRole('button', { name: '引用来源（2）' });
    expect(toggles[0].getAttribute('aria-controls')).not.toBe(toggles[1].getAttribute('aria-controls'));
    await user.click(toggles[0]);
    expect(toggles[0]).toHaveAttribute('aria-expanded', 'true');
    expect(toggles[1]).toHaveAttribute('aria-expanded', 'false');
  });

  it('stays collapsed when sources first arrive or update without an explicit citation selection', () => {
    const { container, rerender } = render(<Sources sources={[]} selection={null} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<Sources sources={sources.slice(0, 1)} selection={null} />);
    expect(screen.getByRole('button', { name: '引用来源（1）' })).toHaveAttribute('aria-expanded', 'false');
    rerender(<Sources sources={sources} selection={null} />);
    expect(screen.getByRole('button', { name: '引用来源（2）' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
  });

  it('still opens and focuses a selected citation, including when selected again after closing', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Sources sources={sources} selection={null} />);
    const toggle = screen.getByRole('button', { name: '引用来源（2）' });
    rerender(<Sources sources={sources} selection={{ index: 2, sequence: 1 }} />);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const card = screen.getByRole('article', { name: '引用 2：资料 2' });
    expect(card).toHaveFocus();
    expect(card).toHaveClass('ring-2');
    expect(card.scrollIntoView).toHaveBeenCalled();
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    rerender(<Sources sources={sources} selection={{ index: 2, sequence: 2 }} />);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(card).toHaveFocus();
  });
});

describe('source card summaries', () => {
  it('cleans Markdown and duplicate titles for display without mutating API data', async () => {
    const source: Source = Object.freeze({ index: 1, doc_id: 'returns', title: '灵码退货条件',
      snippet: '# 灵码退货条件 | 退货申请\n## 屏幕划痕与外观瑕疵的退货条件\n> - 上传照片。  ... ……', score: .9 });
    render(<Sources sources={[source]} selection={null} />);
    await userEvent.setup().click(screen.getByRole('button', { name: '引用来源（1）' }));
    const card = screen.getByRole('article', { name: '引用 1：灵码退货条件' });
    expect(within(card).getByRole('heading')).toHaveTextContent('[1]灵码退货条件');
    expect(within(card).getByText('屏幕划痕与外观瑕疵的退货条件 上传照片。 …')).toBeInTheDocument();
    expect(source.snippet).toBe('# 灵码退货条件 | 退货申请\n## 屏幕划痕与外观瑕疵的退货条件\n> - 上传照片。  ... ……');
  });

  it('keeps a nonempty summary distinct from the title when only a heading was returned', async () => {
    render(<Sources sources={[{ index: 2, doc_id: 'invoice', title: '电子发票', snippet: '# 电子发票', score: 1 }]} selection={null} />);
    await userEvent.setup().click(screen.getByRole('button', { name: '引用来源（1）' }));
    const card = screen.getByRole('article', { name: '引用 2：电子发票' });
    expect(within(card).getByRole('heading')).toHaveTextContent('电子发票');
    expect(within(card).getByText('暂无摘要，请结合回答中的引用查看相关说明。')).toBeInTheDocument();
  });
});
