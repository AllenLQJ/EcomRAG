const messages: Record<number, string> = {
  40301: '访问来源未被允许，请使用正确的访问地址。',
  42901: '提问有点快，休息一分钟再问吧。',
  42902: '今天提问次数已达上限，请明天再试。',
  50401: '回答超时了，请重试一次。',
  40001: '输入有点问题，请检查后重试。',
};

export function errorMessage(code?: number): string {
  return (code && messages[code]) || '服务有点忙，请稍后再试。';
}

export class ChatError extends Error {
  readonly reason?: string;
  readonly limitScope?: 'guest' | 'user';
  readonly retryAfterSeconds?: number;
  constructor(public readonly code: number, public readonly requestId?: string, details?: Record<string, unknown>, public readonly status?: number) {
    super(errorMessage(code));
    this.name = 'ChatError';
    this.reason = typeof details?.message === 'string' ? details.message : undefined;
    this.limitScope = details?.limit_scope === 'guest' || details?.limit_scope === 'user' ? details.limit_scope : undefined;
    this.retryAfterSeconds = typeof details?.retry_after_seconds === 'number' ? details.retry_after_seconds : undefined;
  }
}
