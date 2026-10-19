import { describe, expect, it } from 'vitest';
import { cleanSourceSnippet } from './sourceSnippet';

describe('source snippet display', () => {
  it.each([
    ['## 屏幕划痕与外观瑕疵的退货条件\n> 到货即有的问题\n- 上传照片\n* 等待审核',
      '屏幕划痕与外观瑕疵的退货条件 到货即有的问题 上传照片 等待审核'],
    ['> - ## 退货步骤\n1. 提交申请\n2) 上传材料\n+ 等待审核', '退货步骤 提交申请 上传材料 等待审核'],
    ['\r\n # 灵码退货条件\r\n\r\n ## 退货要求\r\n  签收后  7 天内。', '退货要求 签收后 7 天内。'],
    ['灵码退货条件 | 退货申请\n外观问题需提供照片。', '外观问题需提供照片。'],
    ['灵码退货条件 | 虚构样例\n## 退货条件\n上传材料。', '退货条件 上传材料。'],
    ['灵码退货条件\n# 灵码退货条件\n**质量问题**可申请。', '质量问题可申请。'],
    ['## 保修规则\n\n申请 ... ... …… … 核查。', '保修规则 申请 … 核查。'],
    ['---\n***\n___\n> 请保留订单。', '请保留订单。'],
    ['-5 摄氏度，重量 4.2 克，时效 1–2 个工作日。', '-5 摄氏度，重量 4.2 克，时效 1–2 个工作日。'],
  ])('cleans %s without changing business facts', (snippet, expected) => {
    expect(cleanSourceSnippet('灵码退货条件', snippet)).toBe(expected);
  });

  it('matches full-width title separators and ignores whitespace during comparison', () => {
    expect(cleanSourceSnippet('灵码退货条件｜退货申请', '# 灵码退货条件 | 退货申请\n上传材料。')).toBe('上传材料。');
  });

  it.each(['', ' \n\t', '灵码退货条件', '# 灵码退货条件\n> - *', '... …… …'])
    ('uses an informative fallback when cleanup leaves no summary: %s', snippet => {
      const result = cleanSourceSnippet('灵码退货条件', snippet);
      expect(result).toBe('暂无摘要，请结合回答中的引用查看相关说明。');
      expect(result).not.toContain('灵码退货条件');
    });

  it('keeps useful content when the title is empty', () => {
    expect(cleanSourceSnippet('', '> 请先提交申请。')).toBe('请先提交申请。');
  });
});
