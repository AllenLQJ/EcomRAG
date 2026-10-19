import { useEffect, useRef, useState, type ReactNode, type PointerEvent as ReactPointerEvent } from 'react';

export const SIDEBAR_WIDTH_KEY = 'lingma.sidebarWidth.v1';
const MIN_WIDTH = 200;
const DEFAULT_WIDTH = 260;
const MAX_WIDTH = 420;
const clamp = (width: number) => Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, width));

function loadWidth(): number {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(SIDEBAR_WIDTH_KEY) ?? 'null');
    if (typeof value === 'number' && Number.isFinite(value) && value >= MIN_WIDTH && value <= MAX_WIDTH) return value;
  } catch { /* Storage may be blocked or contain invalid JSON. */ }
  return DEFAULT_WIDTH;
}

export function ResizableSidebar({ children }: { children: ReactNode }) {
  const [width, setWidth] = useState(loadWidth);
  const [dragging, setDragging] = useState(false);
  const stopDrag = useRef<(() => void) | null>(null);

  useEffect(() => {
    try { localStorage.setItem(SIDEBAR_WIDTH_KEY, JSON.stringify(width)); }
    catch { /* Keep resizing available when storage is unavailable. */ }
  }, [width]);
  useEffect(() => () => stopDrag.current?.(), []);

  function startDrag(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || event.isPrimary === false || stopDrag.current) return;
    const handle = event.currentTarget;
    const { pointerId, clientX } = event;
    try { handle.setPointerCapture(pointerId); }
    catch { return; }
    event.preventDefault();
    handle.focus();
    const startWidth = width;
    // Preserve existing inline styles (including priorities), also on unmount.
    const restoreStyles = [document.documentElement, document.body].flatMap(element =>
      ['user-select', '-webkit-user-select', 'cursor'].map(property => {
        const value = element.style.getPropertyValue(property);
        const priority = element.style.getPropertyPriority(property);
        element.style.setProperty(property, property === 'cursor' ? 'col-resize' : 'none');
        return () => {
          if (value) element.style.setProperty(property, value, priority);
          else element.style.removeProperty(property);
        };
      }),
    );
    setDragging(true);

    function move(next: PointerEvent) {
      if (next.pointerId === pointerId) setWidth(clamp(startWidth + next.clientX - clientX));
    }
    function finish() {
      stopDrag.current = null;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
      window.removeEventListener('blur', finish);
      handle.removeEventListener('lostpointercapture', end);
      restoreStyles.forEach(restore => restore());
      setDragging(false);
      try { handle.releasePointerCapture(pointerId); }
      catch { /* Capture may already have been released or the handle removed. */ }
    }
    function end(next: PointerEvent) {
      if (next.pointerId === pointerId) finish();
    }
    stopDrag.current = finish;
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
    window.addEventListener('blur', finish);
    handle.addEventListener('lostpointercapture', end);
  }

  return <aside className="relative h-full shrink-0" style={{ width }}>
    {children}
    <div role="separator" aria-orientation="vertical" aria-label="调整历史栏宽度"
      aria-valuenow={width} aria-valuemin={MIN_WIDTH} aria-valuemax={MAX_WIDTH} tabIndex={0}
      className="sidebar-resizer" data-dragging={dragging || undefined} onPointerDown={startDrag}
      onDoubleClick={() => setWidth(DEFAULT_WIDTH)} onKeyDown={event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        if (event.key === 'Home') setWidth(MIN_WIDTH);
        else if (event.key === 'End') setWidth(DEFAULT_WIDTH);
        else setWidth(current => clamp(current + (event.key === 'ArrowRight' ? 16 : -16)));
      }} />
  </aside>;
}
