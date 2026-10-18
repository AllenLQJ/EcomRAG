import { useCallback, useLayoutEffect, useRef } from 'react';

export function useAutoScroll(change: unknown) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const following = useRef(true);

  const resetFollowing = useCallback(() => { following.current = true; }, []);

  const onScroll = useCallback(() => {
    const element = scrollRef.current;
    if (element) following.current = element.scrollHeight - element.scrollTop - element.clientHeight < 64;
  }, []);

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (following.current && element) element.scrollTop = element.scrollHeight;
  }, [change]);

  // Follow viewport changes (including composer growth), but let the reader expand
  // sources/trace without moving them back to the end of the conversation.
  useLayoutEffect(() => {
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => {
      const element = scrollRef.current;
      if (following.current && element) element.scrollTop = element.scrollHeight;
    });
    if (scrollRef.current) observer.observe(scrollRef.current);
    return () => observer.disconnect();
  }, []);

  return { scrollRef, onScroll, resetFollowing };
}
