import { categoryQuestion, shopInfo } from '../shopInfo';
import { Modal } from './Modal';

export function ShopInfo({ onClose, onCategory }: { onClose: () => void; onCategory: (question: string) => void }) {
  return <Modal title="店铺信息" onClose={onClose}>
    <h3 className="font-serif text-title">{shopInfo.name}</h3>
    <p className="mt-2 text-sm leading-relaxed text-muted">{shopInfo.intro}</p>
    <p className="my-6 rounded-card border border-outline bg-card p-4 text-sm font-medium text-fallback-text">{shopInfo.disclaimer}</p>
    {([['客服信息', shopInfo.support], ['服务承诺', shopInfo.promises]] as const).map(([title, lines]) => <section key={title} className="mb-6">
      <h3 className="mb-2 font-medium">{title}</h3>
      <ul className="list-disc space-y-2 pl-5 text-sm leading-relaxed">{lines.map(line => <li key={line}>{line}</li>)}</ul>
    </section>)}
    <h3 className="mb-3 font-medium">商品品类</h3>
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">{shopInfo.categories.map(category => <button type="button" key={category}
      className="secondary-button min-h-16" onClick={() => onCategory(categoryQuestion(category))}>{category}</button>)}</div>
  </Modal>;
}
