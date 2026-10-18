import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AnswerMarkdown } from '../components/AnswerMarkdown';
import type { Source } from '../api/types';
import { mockMobile } from '../test/media';
import { parseCitations } from './citations';

const sources: Source[] = [1, 2, 10].map(index => ({ index, doc_id: `doc-${index}`, title: `来源 ${index}`, snippet: '', score: 1 }));

describe('citations', () => {
  it('parses adjacent and two-digit indices', () => {
    expect(parseCitations('答案[1][2]，见[10]。', new Set([1, 2, 10]))).toEqual([
      { type: 'text', text: '答案' }, { type: 'citation', index: 1 }, { type: 'citation', index: 2 },
      { type: 'text', text: '，见' }, { type: 'citation', index: 10 }, { type: 'text', text: '。' },
    ]);
  });

  it('keeps unknown numbers as ordinary text', () => {
    expect(parseCitations('[9]接着[1][99]', new Set([1]))).toEqual([
      { type: 'text', text: '[9]接着' }, { type: 'citation', index: 1 }, { type: 'text', text: '[99]' },
    ]);
    expect(parseCitations('[1]', new Set())).toEqual([{ type: 'text', text: '[1]' }]);
  });

  it('renders buttons inside Markdown emphasis and lists, preserving unknown indices', () => {
    const onCitation = vi.fn();
    render(<AnswerMarkdown answer={'**答案[1][2]**\n\n1. 说明[10]，未知[99]'} sources={sources} onCitation={onCitation} />);
    expect(screen.getAllByRole('button')).toHaveLength(3);
    fireEvent.click(screen.getByRole('button', { name: '查看引用 10' }));
    expect(onCitation).toHaveBeenCalledWith(10);
    expect(screen.getByText(/未知\[99\]/)).toBeInTheDocument();
    expect(screen.getByRole('list')).toBeInTheDocument();
  });

  it('does not transform fenced, indented, or inline code', () => {
    const { container } = render(<AnswerMarkdown
      answer={'正文[1]\n\n`[1][2]`\n\n```txt\n[1][10]\n```\n\n    [2]'} sources={sources} onCitation={vi.fn()} />);
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(Array.from(container.querySelectorAll('code')).map(code => code.textContent)).toEqual(['[1][2]', '[1][10]\n', '[2]\n']);
  });

  it('does not render raw HTML or fetch Markdown images', () => {
    const { container } = render(<AnswerMarkdown answer={'<script>alert(1)</script>\n\n![示意图](https://example.test/image.png)'} sources={[]} onCitation={vi.fn()} />);
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
  });
  it('uses a separate mobile group with one touch target per source, keeping inline indices compact', () => {
    mockMobile();
    const onCitation = vi.fn();
    const { container } = render(<AnswerMarkdown answer="说明。[1][2]再次引用[1]" sources={sources} onCitation={onCitation} />);
    expect(screen.getByRole('group', { name: '回答引用' })).toBeInTheDocument();
    expect(container.querySelector('.answer-markdown button')).toBeNull();
    expect(container.querySelectorAll('.answer-markdown sup')).toHaveLength(3);
    expect(screen.getAllByRole('button')).toHaveLength(3);
    const second = screen.getByRole('button', { name: '查看引用 2' });
    expect(second).toHaveClass('min-h-11', 'min-w-11');
    fireEvent.click(second);
    expect(onCitation).toHaveBeenCalledWith(2);
  });

});
