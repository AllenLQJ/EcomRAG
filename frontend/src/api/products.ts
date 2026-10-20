import type { ChatResponse, ProductCard } from './types';
import { isRecord } from './validate';

// Optional cards must not break a valid answer or grow local history without bound.
// 后端 PRODUCT_CARDS_MAX 默认 6、上限 12；前端以后端为准，只做硬上限与体积保护。
export const MAX_PRODUCT_CARDS = 12;
export const MAX_PRODUCTS_BYTES = 16 * 1024;
const shortText = (value: unknown, limit: number): value is string =>
  typeof value === 'string' && value.length <= limit * 2 && !!value.trim() && Array.from(value).length <= limit;

export function normalizeProducts(value: unknown): ProductCard[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const products: ProductCard[] = [];
  const seen = new Set<string>();
  for (const item of value.slice(0, MAX_PRODUCT_CARDS)) {
    if (!isRecord(item) || typeof item.sku !== 'string' || item.sku.length > 64
      || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(item.sku) || seen.has(item.sku)
      || !shortText(item.name, 160) || !shortText(item.category, 64) || !Array.isArray(item.highlights)) continue;
    const highlights = item.highlights.slice(0, 2).filter((text): text is string => shortText(text, 256));
    if (!highlights.length) continue;
    const product: ProductCard = {
      sku: item.sku, name: item.name, category: item.category, highlights,
      image_url: shortText(item.image_url, 2048) ? item.image_url : null,
    };
    if (new TextEncoder().encode(JSON.stringify([...products, product])).byteLength > MAX_PRODUCTS_BYTES) continue;
    products.push(product);
    seen.add(product.sku);
  }
  return products.length ? products : undefined;
}

/** Whitelist optional metadata without rejecting the existing response contract. */
export function withProductCards(response: ChatResponse): ChatResponse {
  const { products: raw, ...answer } = response;
  const products = normalizeProducts(raw);
  return products ? { ...answer, products } : answer;
}
