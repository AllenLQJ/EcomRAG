import type { TraceItem } from '../api/types';
import { traceStageName, traceSummary } from '../lib/format';

export function Trace({ trace }: { trace: TraceItem[] | null }) {
  if (!trace?.length) return null;
  const max = Math.max(1, ...trace.map(item => item.ms));
  return <details className="disclosure-panel px-4">
    <summary className="disclosure-trigger"><span aria-hidden="true" className="disclosure-marker" />链路 · {trace.length} 个阶段</summary>
    <ol className="space-y-3 pb-4" aria-label="链路阶段">
      {trace.map((item, index) => <li key={`${item.stage}-${index}`} className="grid grid-cols-[minmax(0,1fr)_72px] items-center gap-x-3 gap-y-1 text-xs sm:grid-cols-[80px_minmax(0,1fr)_72px]">
        <span className="break-all font-mono">{traceStageName(item.stage)}</span>
        <span aria-hidden="true" className="hidden h-1.5 overflow-hidden rounded-full bg-track sm:block"><span className="block h-full rounded-full bg-ink" style={{ width: `${item.ms / max * 100}%` }} /></span>
        <span className="text-right font-mono text-muted">{Math.round(item.ms).toLocaleString()} ms</span>
        <span aria-hidden="true" className="col-span-2 h-1.5 overflow-hidden rounded-full bg-track sm:hidden"><span className="block h-full rounded-full bg-ink" style={{ width: `${item.ms / max * 100}%` }} /></span>
        <span className="col-span-2 break-words leading-relaxed text-muted sm:col-span-3">{traceSummary(item)}</span>
      </li>)}
    </ol>
  </details>;
}
