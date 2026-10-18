import { ChatError } from '../api/errors';
import type { ChatRequest, ChatResponse, ChatStreamEvent, Source } from '../api/types';
import { createId } from '../lib/storage';

export const ragSources: Source[] = [
  { index: 1, doc_id: 'after_sales_policy.md', title: '售后政策 · 退货条件',
    snippet: '签收之日起 7 天内，商品存在质量问题的，可申请退货，退货运费由店铺承担。', score: 0.94 },
  { index: 2, doc_id: 'after_sales_policy.md', title: '售后政策 · 申请材料',
    snippet: '外观类问题需提供开箱照片或视频，客服在 1–2 个工作日内完成审核。', score: 0.88 },
  { index: 3, doc_id: 'return_guide.md', title: '退货指南 · 申请流程',
    snippet: '在「我的订单」中选择对应商品，点击「申请售后」，选择「退货退款」并填写原因。', score: 0.85 },
];

export function fixtureFor(request: ChatRequest): ChatResponse {
  const common: ChatResponse = {
    request_id: `mock-${createId()}`, session_id: request.session_id,
    answer: '', route: 'rag', strategy: 'direct', sources: [], is_mock: true, trace: null,
  };
  const { message } = request;
  if (message.includes('限流')) throw new ChatError(42901, common.request_id);
  if (message.includes('超时')) throw new ChatError(50401, common.request_id);
  if (message.includes('秒回')) return { ...common, route: 'cache', strategy: null,
    answer: '店铺支持在线申请售后，请在「我的订单」中选择对应商品。[1]',
    sources: [{ index: 1, doc_id: 'faq:12', title: '怎么申请售后？',
      snippet: '在「我的订单」中选择对应商品，点击「申请售后」。', score: 1 }],
    trace: request.debug ? [{ stage: 'cache', ms: 2, hit: true }] : null };
  // Match the more specific product problem before the generic return keyword.
  if (!message.includes('划痕')) {
    if (message.includes('火星')) return { ...common, route: 'fallback', strategy: null,
      answer: '抱歉，店铺资料里没有找到相关说明。为避免给你不准确的信息，建议联系人工客服确认。' };
    if (message.includes('你好')) return { ...common, route: 'general', strategy: null,
      answer: '你好，很高兴为你服务。售后、配送、发票、商品参数，都可以问我。' };
    if (message.includes('退货')) return { ...common, route: 'faq', strategy: null,
      answer: '在「我的订单」中找到该商品，点击「申请售后」，选择「退货退款」并填写原因即可。[1]',
      sources: [{ index: 1, doc_id: 'faq:12', title: '怎么申请退货？',
        snippet: '在「我的订单」中找到该商品，点击「申请售后」，选择「退货退款」并填写原因即可。', score: 0.98 }] };
  }
  return { ...common,
    answer: '可以。签收后 **7 天内**，商品存在质量问题可申请退货，运费由店铺承担[1]。如果划痕是到货时就有的外观瑕疵，申请时请上传开箱照片，审核通常在 1–2 个工作日内完成。[1][2]\n\n1. 在「我的订单」里找到对应商品。\n2. 点击「申请售后」，选择「退货退款」并填写原因。[3]\n\n以上为虚构店铺的样例政策，实际售后请以店铺确认为准。',
    sources: ragSources.map(source => ({ ...source })),
    trace: request.debug ? [
      { stage: 'cache', ms: 2, hit: false },
      { stage: 'faq', ms: 35, hit: false, top_score: 0.61 },
      { stage: 'router', ms: 90, intent: 'knowledge', strategy: 'direct' },
      { stage: 'retrieve', ms: 120, candidates: 10 },
      { stage: 'rerank', ms: 180, kept: 3 },
      { stage: 'generate', ms: 310, detail: '引用 3 · 本地模拟耗时' },
    ] : null,
  };
}

export async function mockChat(request: ChatRequest, signal?: AbortSignal): Promise<ChatResponse> {
  await new Promise<void>((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException('Cancelled', 'AbortError')); return; }
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      reject(new DOMException('Cancelled', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort);
      resolve();
    }, 600 + Math.floor(Math.random() * 901));
    signal?.addEventListener('abort', abort, { once: true });
  });
  return fixtureFor(request);
}

function streamPause(signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new DOMException('Cancelled', 'AbortError')); return; }
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      reject(new DOMException('Cancelled', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort);
      resolve();
    }, 30 + Math.floor(Math.random() * 31));
    signal?.addEventListener('abort', abort, { once: true });
  });
}

export async function mockChatStream(
  request: ChatRequest, emit: (event: ChatStreamEvent) => void, signal?: AbortSignal,
): Promise<ChatResponse> {
  if (signal?.aborted) throw new DOMException('Cancelled', 'AbortError');
  const requestId = `mock-${createId()}`;
  emit({ event: 'meta', data: { request_id: requestId, session_id: request.session_id } });
  emit({ event: 'status', data: { stage: 'faq' } });
  await streamPause(signal);
  let response: ChatResponse;
  try { response = { ...fixtureFor(request), request_id: requestId }; }
  catch (error) {
    const failure = error instanceof ChatError ? error : new ChatError(50001, requestId);
    emit({ event: 'error', data: { code: failure.code, message: failure.message, request_id: requestId } });
    throw failure;
  }
  if (!['cache', 'faq'].includes(response.route)) emit({ event: 'status', data: { stage: 'routing' } });
  if (response.route === 'rag') emit({ event: 'status', data: { stage: 'retrieving' } });
  if (response.route === 'rag' || response.route === 'general') emit({ event: 'status', data: { stage: 'generating' } });
  for (const text of Array.from(response.answer)) {
    await streamPause(signal);
    emit({ event: 'delta', data: { text } });
  }
  emit({ event: 'done', data: response });
  return response;
}
