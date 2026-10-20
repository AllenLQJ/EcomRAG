import { categoryQuestion, shopInfo } from '../shopInfo';
import { Modal } from './Modal';
import { LogoMark } from './Brand';

export function ShopInfo({ onClose, onCategory }: { onClose: () => void; onCategory: (question: string) => void }) {
  return <Modal title="店铺商品" onClose={onClose} header={<div className="shop-products-brand flex min-w-0 flex-1 items-start gap-3">
    <LogoMark className="h-10 w-10 sm:h-14 sm:w-14" />
    <div className="min-w-0">
      <h2 className="font-serif text-title">{shopInfo.name}</h2>
      <p className="mt-2 text-sm leading-relaxed text-muted">{shopInfo.intro}</p>
    </div>
  </div>}>
    <h3 className="mb-3 font-medium">商品品类</h3>
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">{shopInfo.categories.map(category => <button type="button" key={category}
      className="secondary-button min-h-16" onClick={() => onCategory(categoryQuestion(category))}>{category}</button>)}</div>
  </Modal>;
}
