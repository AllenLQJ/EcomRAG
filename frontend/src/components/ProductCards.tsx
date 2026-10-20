import { useState } from 'react';
import type { ProductCard } from '../api/types';
import { normalizeProducts } from '../api/products';
import { productImageUrl } from '../lib/productImages';

function ProductImage({ product }: { product: ProductCard }) {
  const src = productImageUrl(product.image_url, product.sku);
  const [failedSrc, setFailedSrc] = useState<string>();
  return <div className="flex aspect-[4/3] w-full shrink-0 items-center justify-center overflow-hidden bg-canvas dark:bg-body-muted">
    {src && src !== failedSrc
      ? <img src={src} alt={product.name} loading="lazy" decoding="async" width={400} height={300}
        className="h-full w-full object-contain" onError={() => setFailedSrc(src)} />
      : <div role="img" aria-label={`${product.name}（暂无图片）`} className="flex flex-col items-center gap-2 text-muted dark:text-outline">
        <svg aria-hidden="true" viewBox="0 0 32 32" fill="none" className="h-8 w-8" stroke="currentColor" strokeWidth="1.5">
          <rect x="4" y="5" width="24" height="22" rx="3" />
          <circle cx="11" cy="12" r="2" /><path d="m5 23 7-7 5 5 4-4 6 6" />
        </svg>
        <span className="text-xs">暂无图片</span>
      </div>}
  </div>;
}

export function ProductCards({ products }: { products?: ProductCard[] }) {
  const cards = normalizeProducts(products);
  if (!cards) return null;
  return <section aria-label="资料关联商品" className="min-w-0 max-w-full">
    <p className="mb-2 text-xs text-muted dark:text-outline">资料关联商品</p>
    <ul className="grid auto-rows-fr grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {cards.map(product => <li key={product.sku} className="flex h-full min-w-0 flex-col overflow-hidden rounded-card border border-line bg-card text-ink dark:border-muted dark:bg-ink dark:text-card">
        <ProductImage product={product} />
        <div className="min-w-0 space-y-2 break-words p-3">
          <h3 className="break-words text-sm font-semibold">{product.name}</h3>
          <p className="text-xs text-muted dark:text-outline">{product.category}</p>
          <ul className="list-disc space-y-1 break-words pl-4 text-xs leading-relaxed text-body-muted dark:text-line">
            {product.highlights.map((highlight, index) => <li key={index}>{highlight}</li>)}
          </ul>
        </div>
      </li>)}
    </ul>
  </section>;
}
