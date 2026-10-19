import { useEffect, useState } from 'react';

export function useDesktop(): boolean {
  const [desktop, setDesktop] = useState(() => window.matchMedia?.('(min-width: 1024px)').matches ?? true);
  useEffect(() => {
    const query = window.matchMedia?.('(min-width: 1024px)');
    if (!query) return;
    const update = () => setDesktop(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return desktop;
}
