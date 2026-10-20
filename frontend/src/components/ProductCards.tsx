import { useId, useState } from 'react';
import type { ProductCard } from '../api/types';
import { normalizeProducts } from '../api/products';
import { productImageUrl } from '../lib/productImages';

function ProductImage({ product }: { product: ProductCard }) {
  const src = productImageUrl(product.image_url, product.sku);
  const [failedSrc, setFailedSrc] = useState<string>();
  return <div className="product-card-image flex aspect-square w-full shrink-0 items-center justify-center overflow-hidden bg-canvas dark:bg-body-muted">
    {src && src !== failedSrc
      ? <img src={src} alt={product.name} loading="lazy" decoding="async" width={400} height={400}
        className="h-full w-full object-cover" onError={() => setFailedSrc(src)} />
      : <div role="img" aria-label={`${product.name}（暂无图片）`} className="flex flex-col items-center gap-2 text-muted dark:text-outline">
        <svg aria-hidden="true" viewBox="0 0 32 32" fill="none" className="h-8 w-8" stroke="currentColor" strokeWidth="1.5">
          <rect x="4" y="5" width="24" height="22" rx="3" />
          <circle cx="11" cy="12" r="2" /><path d="m5 23 7-7 5 5 4-4 6 6" />
        </svg>
        <span className="text-xs">暂无图片</span>
      </div>}
  </div>;
}

function ProductCardItem({ product }: { product: ProductCard }) {
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  return <li className="flex min-w-0 flex-col overflow-hidden rounded-card border border-line dark:border-muted">
    <ProductImage product={product} />
    <div className="product-card-info min-w-0 break-words p-3">
      <h3>
        <button type="button" aria-expanded={expanded} aria-controls={detailsId}
          className="product-card-toggle flex min-h-11 w-full items-center gap-2 text-left text-sm font-semibold"
          onClick={() => setExpanded(value => !value)}>
          <span className="min-w-0 flex-1 truncate" title={product.name}>{product.name}</span>
          <svg aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"
            className={`h-4 w-4 shrink-0 ${expanded ? 'rotate-180' : ''}`}>
            <path d="m4 6 4 4 4-4" />
          </svg>
        </button>
      </h3>
      <div id={detailsId} hidden={!expanded} className="space-y-2 pt-2">
        <p className="text-xs text-muted">{product.category}</p>
        <ul className="list-disc space-y-1 break-words pl-4 text-xs leading-relaxed text-body-muted">
          {product.highlights.map((highlight, index) => <li key={index}>{highlight}</li>)}
        </ul>
      </div>
    </div>
  </li>;
}

export function ProductCards({ products }: { products?: ProductCard[] }) {
  const cards = normalizeProducts(products);
  if (!cards) return null;
  return <section aria-label="资料关联商品" className="min-w-0 max-w-full">
    <p className="mb-2 text-xs text-muted dark:text-outline">资料关联商品</p>
    <ul className="grid grid-cols-1 items-start gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {cards.map(product => <ProductCardItem key={product.sku} product={product} />)}
    </ul>
  </section>;
}
