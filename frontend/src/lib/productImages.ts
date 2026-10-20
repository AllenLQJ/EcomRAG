/** Relative paths use the image host, never the chat API or current page path. */
export function productImageUrl(imageUrl: string | null, sku: string): string | undefined {
  if (!imageUrl?.trim()) return undefined;
  try {
    if (/^https?:\/\//i.test(imageUrl)) {
      const url = new URL(imageUrl);
      return !url.username && !url.password ? url.href : undefined;
    }
    if (!['webp', 'png', 'jpg', 'jpeg', 'svg'].some(ext => imageUrl === `${sku}/main.${ext}`)) return undefined;
    const base = import.meta.env.VITE_PRODUCT_IMAGE_BASE_URL?.trim();
    if (!base) return undefined;
    const sameOrigin = base.startsWith('/') && !base.startsWith('//');
    if (!sameOrigin && !/^https?:\/\//i.test(base)) return undefined;
    const root = new URL(base, window.location.origin);
    if (!['http:', 'https:'].includes(root.protocol) || root.username || root.password || root.search || root.hash) return undefined;
    root.pathname = `${root.pathname.replace(/\/+$/, '')}/`;
    const resolved = new URL(imageUrl, root);
    return sameOrigin ? resolved.pathname : resolved.href;
  } catch { return undefined; }
}
