import { useEffect, useId, useState } from 'react';
import type { ProductCard } from '../api/types';
import { normalizeProducts } from '../api/products';
import { productImageUrl } from '../lib/productImages';

// Keep these queries aligned with the grid's sm/lg breakpoints (covered by tests).
const TWO_COLUMNS = '(min-width: 640px)';
const THREE_COLUMNS = '(min-width: 1024px)';
const viewportColumns = () => window.matchMedia?.(THREE_COLUMNS).matches ? 3
  : window.matchMedia?.(TWO_COLUMNS).matches ? 2 : 1;

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

function ProductCardItem({ product, expanded, onToggle }: {
  product: ProductCard;
  expanded: boolean;
  onToggle: () => void;
}) {
  const detailsId = useId();
  return <li className="disclosure-panel flex min-w-0 flex-col overflow-hidden">
    <ProductImage product={product} />
    <div className="product-card-info min-w-0 break-words px-4">
      <h3>
        <button type="button" aria-expanded={expanded} aria-controls={detailsId}
          className="disclosure-trigger"
          onClick={onToggle}>
          <span aria-hidden="true" className="disclosure-marker" />
          <span className="min-w-0 flex-1 truncate" title={product.name}>{product.name}</span>
        </button>
      </h3>
      <div id={detailsId} hidden={!expanded} className="space-y-2 pb-4">
        <p className="text-xs text-muted">{product.category}</p>
        <ul className="list-disc space-y-1 break-words pl-4 text-xs leading-relaxed text-body-muted">
          {product.highlights.map((highlight, index) => <li key={index}>{highlight}</li>)}
        </ul>
      </div>
    </div>
  </li>;
}

function ProductGrid({ cards }: { cards: ProductCard[] }) {
  const [layout, setLayout] = useState(() => ({ columns: viewportColumns(), expandedRows: new Set<string>() }));
  useEffect(() => {
    const two = window.matchMedia?.(TWO_COLUMNS);
    const three = window.matchMedia?.(THREE_COLUMNS);
    const update = () => {
      const columns = three?.matches ? 3 : two?.matches ? 2 : 1;
      // A different column count changes row membership; start the new rows collapsed.
      setLayout(previous => previous.columns === columns ? previous : { columns, expandedRows: new Set<string>() });
    };
    update();
    two?.addEventListener('change', update);
    three?.addEventListener('change', update);
    return () => {
      two?.removeEventListener('change', update);
      three?.removeEventListener('change', update);
    };
  }, []);
  const rows: ProductCard[][] = [];
  for (let start = 0; start < cards.length; start += layout.columns) {
    rows.push(cards.slice(start, start + layout.columns));
  }
  const toggleRow = (rowKey: string) => setLayout(previous => {
    const expandedRows = new Set(previous.expandedRows);
    if (expandedRows.has(rowKey)) expandedRows.delete(rowKey);
    else expandedRows.add(rowKey);
    return { ...previous, expandedRows };
  });
  return <section aria-label="资料关联商品" className="min-w-0 max-w-full">
    <p className="mb-2 text-xs text-muted dark:text-outline">资料关联商品</p>
    <ul className="grid grid-cols-1 items-stretch gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {rows.flatMap(row => {
        const rowKey = row.map(product => product.sku).join(',');
        return row.map(product => <ProductCardItem key={product.sku} product={product}
          expanded={layout.expandedRows.has(rowKey)} onToggle={() => toggleRow(rowKey)} />);
      })}
    </ul>
  </section>;
}

export function ProductCards({ products }: { products?: ProductCard[] }) {
  const cards = normalizeProducts(products);
  return cards ? <ProductGrid cards={cards} /> : null;
}
