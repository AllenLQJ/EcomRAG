import { logoVariant } from '../lib/config';

export function LogoMark({ className = 'h-7 w-7', variant = logoVariant() }: { className?: string; variant?: string }) {
  return <img src={`/brand/lingma-${variant}.svg`} alt="" aria-hidden="true" draggable={false} className={`${className} shrink-0 select-none`} />;
}

export function LogoLockup({ className = 'h-14' }: { className?: string }) {
  return <img src="/brand/lingma-d-lockup.svg" alt="" aria-hidden="true" draggable={false} className={`${className} w-auto select-none`} />;
}

export function AssistantAvatar() {
  return <LogoMark className="h-8 w-8" variant="c-dialog" />;
}

export function SendIcon() {
  return <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
    <path d="M9 14V4m0 0L4.5 8.5M9 4l4.5 4.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
  </svg>;
}

export function Chevron({ open }: { open: boolean }) {
  return <svg className={open ? 'rotate-180' : ''} width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
    <path d="m3 4.5 3 3 3-3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
  </svg>;
}
