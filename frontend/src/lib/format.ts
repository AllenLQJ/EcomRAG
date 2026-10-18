export function characterCount(value: string): number {
  // Match Python/Pydantic's Unicode code point count, including emoji.
  return Array.from(value).length;
}

export function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} 秒`;
}

export function traceSummary(item: Record<string, unknown>): string {
  return Object.entries(item)
    .filter(([key, value]) => key !== 'stage' && key !== 'ms' && value !== null && value !== undefined)
    .map(([key, value]) => {
      const summary = typeof value === 'object' ? JSON.stringify(value) : String(value);
      return `${key}: ${summary}`;
    }).join(' · ');
}
