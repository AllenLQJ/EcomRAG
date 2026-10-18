import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it } from 'vitest';
import { Trace } from './Trace';

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
