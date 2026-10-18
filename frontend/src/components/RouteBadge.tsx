import type { Route } from '../api/types';

const routes: Record<Route, { label: string; badge: string; dot: string }> = {
  cache: { label: '秒回', badge: 'bg-cache-bg text-cache-text border-transparent', dot: 'bg-cache-dot' },
  faq: { label: '常见问题', badge: 'bg-faq-bg text-faq-text border-transparent', dot: 'bg-faq-dot' },
  rag: { label: '知识库回答', badge: 'bg-card text-ink border-line', dot: 'bg-ink' },
  general: { label: '闲聊', badge: 'bg-general-bg text-general-text border-transparent', dot: 'bg-general-dot' },
  fallback: { label: '未找到答案', badge: 'bg-fallback-bg text-fallback-text border-transparent', dot: 'bg-fallback-dot' },
};

export function RouteBadge({ route }: { route: Route }) {
  const config = routes[route];
  return <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${config.badge}`}>
    <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${config.dot}`} />{config.label}
  </span>;
}
