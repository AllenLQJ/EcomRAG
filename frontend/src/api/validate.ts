import type { ChatResponse } from './types';

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function isChatResponse(value: unknown): value is ChatResponse {
  if (!isRecord(value)) return false;
  return typeof value.request_id === 'string' && typeof value.session_id === 'string'
    && typeof value.answer === 'string'
    && ['cache', 'faq', 'rag', 'general', 'fallback'].includes(String(value.route))
    && (value.strategy === null || ['direct', 'hyde', 'subquery', 'stepback'].includes(String(value.strategy)))
    && typeof value.is_mock === 'boolean'
    && Array.isArray(value.sources) && value.sources.every(source =>
      isRecord(source) && Number.isInteger(source.index) && Number(source.index) > 0
      && typeof source.doc_id === 'string' && typeof source.title === 'string'
      && typeof source.snippet === 'string' && typeof source.score === 'number' && Number.isFinite(source.score))
    && (value.trace === null || (Array.isArray(value.trace) && value.trace.every(item =>
      isRecord(item) && typeof item.stage === 'string' && typeof item.ms === 'number'
      && Number.isFinite(item.ms) && item.ms >= 0)));
}
