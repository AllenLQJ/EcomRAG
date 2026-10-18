export function humanSupportUrl(): string | undefined {
  const value = import.meta.env.VITE_HUMAN_SUPPORT_URL?.trim();
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
}

export function storeName(): string {
  return import.meta.env.VITE_STORE_NAME?.trim() || '栖木';
}
