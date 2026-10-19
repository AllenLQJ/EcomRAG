import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { Source } from '../api/types';
import { Sources } from './Sources';

describe('source card summaries', () => {
  it('cleans Markdown and duplicate titles for display without mutating API data', () => {
    const source: Source = Object.freeze({ index: 1, doc_id: 'returns', title: '灵码退货条件',
      snippet: '# 灵码退货条件 | 退货申请\n## 屏幕划痕与外观瑕疵的退货条件\n> - 上传照片。  ... ……', score: .9 });
    render(<Sources sources={[source]} selection={null} />);
    const card = screen.getByRole('article', { name: '引用 1：灵码退货条件' });
    expect(within(card).getByRole('heading')).toHaveTextContent('[1]灵码退货条件');
    expect(within(card).getByText('屏幕划痕与外观瑕疵的退货条件 上传照片。 …')).toBeInTheDocument();
    expect(source.snippet).toBe('# 灵码退货条件 | 退货申请\n## 屏幕划痕与外观瑕疵的退货条件\n> - 上传照片。  ... ……');
  });

  it('keeps a nonempty summary distinct from the title when only a heading was returned', () => {
    render(<Sources sources={[{ index: 2, doc_id: 'invoice', title: '电子发票', snippet: '# 电子发票', score: 1 }]} selection={null} />);
    const card = screen.getByRole('article', { name: '引用 2：电子发票' });
    expect(within(card).getByRole('heading')).toHaveTextContent('电子发票');
    expect(within(card).getByText('暂无摘要，请结合回答中的引用查看相关说明。')).toBeInTheDocument();
  });
});
