import { useEffect, useState } from 'react';
import { nextLocalMidnight } from '../lib/conversationTime';

/** Refresh relative labels every minute, with a separate timer at the local date boundary. */
export function useConversationClock(): number {
  const [clock, setClock] = useState(() => ({ now: Date.now() }));
  useEffect(() => {
    let midnight: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      const timestamp = Date.now();
      // A timezone change can require regrouping even at the same timestamp.
      setClock({ now: timestamp });
      clearTimeout(midnight);
      // Recompute in the current timezone after clock changes, wake-up and every minute.
      midnight = setTimeout(refresh, Math.max(1, nextLocalMidnight(timestamp) - timestamp));
    };
    const visible = () => { if (document.visibilityState !== 'hidden') refresh(); };
    refresh();
    const minute = setInterval(refresh, 60_000);
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', visible);
    return () => {
      clearTimeout(midnight);
      clearInterval(minute);
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', visible);
    };
  }, []);
  return clock.now;
}
