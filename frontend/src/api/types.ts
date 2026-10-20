export type Route = 'cache' | 'faq' | 'rag' | 'general' | 'fallback';
export type Strategy = 'direct' | 'hyde' | 'subquery' | 'stepback';
export type Intent = 'general' | 'knowledge' | 'sensitive';

export interface HistoryMessage { role: 'user' | 'assistant'; content: string }

export interface ChatRequest {
  session_id: string;
  message: string;
  debug: boolean;
  history?: HistoryMessage[];
  conversation_id?: string;
}

export interface Source {
  index: number;
  doc_id: string;
  title: string;
  snippet: string;
  score: number;
}

export interface ProductCard {
  sku: string;
  name: string;
  category: string;
  highlights: string[];
  image_url: string | null;
}

export interface TraceItem {
  stage: string;
  ms: number;
  route?: Route | null;
  strategy?: Strategy | null;
  detail?: string | null;
  hit?: boolean | null;
  top_score?: number | null;
  intent?: Intent | null;
  candidates?: number | null;
  kept?: number | null;
  speculative?: boolean | null;
  [key: string]: unknown;
}

export interface ChatResponse {
  request_id: string;
  session_id: string;
  answer: string;
  route: Route;
  strategy: Strategy | null;
  sources: Source[];
  is_mock: boolean;
  trace: TraceItem[] | null;
  products?: ProductCard[];
}

export interface ErrorResponse {
  code: number;
  message: string;
  request_id?: string;
  limit_scope?: 'guest' | 'user';
  retry_after_seconds?: number;
}

export type ChatMessage =
  | { id: string; kind: 'user'; text: string }
  | { id: string; kind: 'assistant'; response: ChatResponse; elapsedMs: number; question: string }
  | { id: string; kind: 'error'; code: number; requestId?: string; question: string; limitScope?: 'guest' | 'user' };

export type StreamStage = 'faq' | 'routing' | 'retrieving' | 'generating';
export type ChatStreamEvent =
  | { event: 'meta'; data: { request_id: string; session_id: string } }
  | { event: 'status'; data: { stage: StreamStage } }
  | { event: 'delta'; data: { text: string } }
  | { event: 'done'; data: ChatResponse }
  | { event: 'error'; data: ErrorResponse };
export interface StreamProgress { text: string; stage: StreamStage }
