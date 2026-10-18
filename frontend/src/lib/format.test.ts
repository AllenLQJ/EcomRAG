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

  it.each([
    ['rag', '知识库回答'], ['faq', '常见问题'], ['cache', '秒回'], ['general', '闲聊'], ['fallback', '未找到答案'],
  ])('maps route %s', (route, label) => {
    expect(traceSummary({ route })).toBe(`来源: ${label}`);
  });

  it.each([[true, '是'], [false, '否']] as const)('maps speculative %s', (speculative, label) => {
    expect(traceSummary({ speculative })).toBe(`推测检索: ${label}`);
  });

  it('rounds scores and floating point values to two places and keeps counts integral', () => {
    expect(traceSummary({ top_score: 0.605768371524513, candidates: 10, kept: 3, confidence: 0.333333333 }))
      .toBe('最高分: 0.61 · 候选数: 10 · 保留数: 3 · confidence: 0.33');
    expect(traceSummary({ top_score: 1 })).toBe('最高分: 1.00');
    expect(traceSummary({ top_score: 0 })).toBe('最高分: 0.00');
    expect(traceSummary({ top_score: 0.004 })).toBe('最高分: 0.00');
    expect(traceSummary({ top_score: '0.6057' })).toBe('最高分: 0.6057');
  });

  it('preserves unknown fields, stages and enum values', () => {
    expect(traceSummary({ future: { safe: 1 }, intent: 'future', strategy: 'future', route: 'future', constructor: 'raw' }))
      .toBe('future: {"safe":1} · 意图: future · 策略: future · 来源: future · constructor: raw');
  });
});
