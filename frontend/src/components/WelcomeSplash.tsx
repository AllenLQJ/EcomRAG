import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { storeName, welcomeEnabled } from '../lib/config';

export const WELCOME_KEY = 'lingma.welcome.v1';
const DEFAULT_DURATION = 2300;
type Phase = 'hidden' | 'playing' | 'exiting';

function hasPlayed(): boolean {
  try { return sessionStorage.getItem(WELCOME_KEY) === '1'; }
  catch { return false; }
}

function markPlayed(): void {
  try { sessionStorage.setItem(WELCOME_KEY, '1'); }
  catch { /* The animation still works when storage is blocked or full. */ }
}

export function WelcomeSplash({ enabled = welcomeEnabled(), duration = DEFAULT_DURATION }: { enabled?: boolean; duration?: number }) {
  const [phase, setPhase] = useState<Phase>('hidden');
  const started = useRef(false);
  const name = storeName();
  const exit = useCallback(() => setPhase(current => current === 'playing' ? 'exiting' : current), []);

  useLayoutEffect(() => {
    if (!enabled) { setPhase('hidden'); return; }
    const motion = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    const start = () => {
      if (motion?.matches) {
        started.current = true;
        markPlayed();
        setPhase('hidden');
        return;
      }
      // Own this playback across StrictMode's effect cleanup/setup cycle.
      if (started.current || hasPlayed()) return;
      started.current = true;
      markPlayed();
      setPhase('playing');
    };
    start();
    motion?.addEventListener('change', start);
    return () => motion?.removeEventListener('change', start);
  }, [enabled]);

  useEffect(() => {
    if (!enabled || phase !== 'playing') return;
    const timer = window.setTimeout(exit, Number.isFinite(duration) && duration >= 0 ? duration : DEFAULT_DURATION);
    const keydown = (event: KeyboardEvent) => {
      if (!['Escape', 'Enter', ' ', 'Spacebar'].includes(event.key)) return;
      // Capture before the focused composer can submit or insert a space.
      event.preventDefault();
      event.stopPropagation();
      exit();
    };
    window.addEventListener('keydown', keydown, true);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener('keydown', keydown, true);
    };
  }, [enabled, phase, duration, exit]);

  useEffect(() => {
    if (!enabled || phase !== 'exiting') return;
    // animationend normally removes the layer after 560ms; also handle missing events.
    const timer = window.setTimeout(() => setPhase('hidden'), 700);
    return () => window.clearTimeout(timer);
  }, [enabled, phase]);

  if (!enabled || phase === 'hidden') return null;
  return <div role="dialog" aria-label={`欢迎来到${name}`} aria-modal="false"
    className="welcome-splash" data-phase={phase} onClick={exit}
    onPointerDown={event => event.preventDefault()}
    onAnimationEnd={event => {
      if (phase === 'exiting' && event.target === event.currentTarget && event.animationName === 'welcome-exit') setPhase('hidden');
    }}>
    <div className="welcome-content">
      <p className="welcome-label">{name} · 智能客服</p>
      <div className="welcome-logo">
        <svg width="88" height="88" viewBox="0 0 120 120" aria-hidden="true" focusable="false">
          <rect x="6" y="6" width="108" height="108" rx="26" className="fill-ink" />
          <g transform="translate(12 10) scale(0.8)">
            <path d="M34 24H82M42 39H82M30 54H82M82 24V54M38 68L45 80M60 60C60 80 52 94 32 104M62 80C70 92 78 99 90 104"
              fill="none" className="stroke-canvas" strokeWidth="8" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx="79" cy="72" r="6" className="welcome-spark fill-spark" />
          </g>
        </svg>
        <span className="welcome-ripple" aria-hidden="true" />
      </div>
      <h2 className="welcome-title">欢迎来到{name}</h2>
      <div className="welcome-rule" aria-hidden="true" />
      <p className="welcome-subtitle">让每一次提问，都有答案</p>
    </div>
    <p className="welcome-skip">点击任意处跳过</p>
  </div>;
}
