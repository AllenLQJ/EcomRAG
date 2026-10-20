/** One shop action for the header and the mobile composer disclosure row. */
export function ShopInfoButton({ onClick }: { onClick: () => void }) {
  return <button type="button" className="shop-info-button" onClick={onClick} aria-haspopup="dialog">
    店铺信息
  </button>;
}
