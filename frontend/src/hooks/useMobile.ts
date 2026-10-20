import { useEffect, useState } from 'react';

const MOBILE_QUERY = '(max-width: 639px)';

/** Shared breakpoint for behavior that CSS alone cannot express. */
export function useMobile(): boolean {
  const [mobile, setMobile] = useState(() => window.matchMedia?.(MOBILE_QUERY).matches ?? false);
  useEffect(() => {
    const query = window.matchMedia?.(MOBILE_QUERY);
    if (!query) return;
    const update = () => setMobile(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return mobile;
}
