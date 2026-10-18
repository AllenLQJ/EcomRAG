import { describe, expect, it } from 'vitest';
import { traceStageName, traceSummary } from './format';

describe('trace labels', () => {
  it.each([
    ['cache', '缓存'], ['faq', '常见问题'], ['router', '路由'], ['retrieve', '检索'],
    ['rerank', '重排'], ['generate', '生成'], ['total', '总计'], ['future', 'future'],
    ['constructor', 'constructor'],
  ])('maps stage %s to %s', (stage, label) => {
    expect(traceStageName(stage)).toBe(label);
  });

  it('maps known fields and values while omitting absent fields', () => {
    expect(traceSummary({ stage: 'router', ms: 1, hit: false, top_score: 0.61, intent: 'knowledge',
      strategy: 'direct', candidates: 10, kept: 3, detail: '本地模拟耗时', route: 'cache', speculative: true,
      absent: null, missing: undefined,
    })).toBe('命中: 否 · 最高分: 0.61 · 意图: 知识库 · 策略: 直接检索 · 候选数: 10 · 保留数: 3 · 说明: 本地模拟耗时 · 来源: 秒回 · 推测检索: 是');
    expect(traceSummary({ hit: true })).toBe('命中: 是');
  });

  it.each([['general', '闲聊'], ['knowledge', '知识库'], ['sensitive', '敏感']])('maps intent %s', (intent, label) => {
    expect(traceSummary({ intent })).toBe(`意图: ${label}`);
  });

  it('preserves unknown fields, stages and enum values', () => {
    expect(traceSummary({ future: { safe: 1 }, intent: 'future', strategy: 'future' }))
      .toBe('future: {"safe":1} · 意图: future · 策略: future');
  });
});
