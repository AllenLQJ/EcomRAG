import { AuthForm } from './AuthForm';
import { LogoMark } from './Brand';
import type { User } from '../api/account';
import { brandSlogan, storeName } from '../lib/config';
import { shopInfo } from '../shopInfo';

/** Shared brand, card and form for both entry and sidebar authentication. */
export function AuthPanel({ onSuccess, onGuest }: { onSuccess: (user: User) => void; onGuest?: () => void }) {
  return <div className="mx-auto w-full max-w-[360px]">
    <header className="entry-brand">
      <LogoMark variant="b-seal" className="entry-logo" />
      <h1 className="entry-store-name">{storeName()}</h1>
      <p className="entry-slogan">{brandSlogan()}</p>
      <p className="entry-service-label"><span aria-hidden="true" />智能客服<span aria-hidden="true" /></p>
    </header>
    <div className="entry-card"><AuthForm onSuccess={onSuccess} onGuest={onGuest} /></div>
    <p className="entry-footer">{shopInfo.disclaimer}</p>
  </div>;
}
