export function characterCount(value: string): number {
  // Match Python/Pydantic's Unicode code point count, including emoji.
  return Array.from(value).length;
}

export function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} 秒`;
}

const stages: Record<string, string> = {
  cache: '缓存', faq: '常见问题', router: '路由', retrieve: '检索', rerank: '重排', generate: '生成', total: '总计',
};
const fields: Record<string, string> = {
  hit: '命中', top_score: '最高分', intent: '意图', strategy: '策略', candidates: '候选数',
  kept: '保留数', detail: '说明', route: '来源', speculative: '推测检索',
};
const intents: Record<string, string> = { general: '闲聊', knowledge: '知识库', sensitive: '敏感' };
const strategies: Record<string, string> = { direct: '直接检索', hyde: '假设文档', subquery: '子问题', stepback: '回溯' };
const routes: Record<string, string> = { cache: '秒回', faq: '常见问题', rag: '知识库回答', general: '闲聊', fallback: '未找到答案' };
const lookup = (labels: Record<string, string>, value: string): string => Object.hasOwn(labels, value) ? labels[value] : value;

export function traceStageName(stage: string): string {
  return lookup(stages, stage);
}

export function traceSummary(item: Record<string, unknown>): string {
  return Object.entries(item)
    .filter(([key, value]) => key !== 'stage' && key !== 'ms' && value !== null && value !== undefined)
    .map(([key, value]) => {
      let summary = typeof value === 'object' ? JSON.stringify(value) : String(value);
      if (typeof value === 'number' && Number.isFinite(value)) {
        summary = key === 'top_score' || !Number.isInteger(value) ? value.toFixed(2) : String(value);
      } else if (key === 'hit' || key === 'speculative') {
        if (typeof value === 'boolean') summary = value ? '是' : '否';
      } else if (typeof value === 'string') {
        if (key === 'intent') summary = lookup(intents, value);
        if (key === 'strategy') summary = lookup(strategies, value);
        if (key === 'route') summary = lookup(routes, value);
      }
      return `${lookup(fields, key)}: ${summary}`;
    }).join(' · ');
}
