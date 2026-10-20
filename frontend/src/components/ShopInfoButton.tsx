/** One floating shop entry, outside the composer controls at every viewport width. */
export function ShopInfoButton({ onClick }: { onClick: () => void }) {
  return <button type="button" className="shop-products-button" onClick={onClick} aria-haspopup="dialog">
    <span>店铺商品</span><span aria-hidden="true" className="shop-products-dot" />
  </button>;
}
