import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ResizableSidebar, SIDEBAR_WIDTH_KEY } from './ResizableSidebar';
import { pointer } from '../test/pointer';

function mount() {
  const view = render(<ResizableSidebar><p>历史内容</p></ResizableSidebar>);
  const handle = screen.getByRole('separator', { name: '调整历史栏宽度' });
  const capture = vi.fn();
  const release = vi.fn();
  Object.defineProperties(handle, {
    setPointerCapture: { configurable: true, value: capture },
    releasePointerCapture: { configurable: true, value: release },
  });
  return { ...view, handle, capture, release };
}

function expectWidth(width: number) {
  expect(screen.getByRole('complementary')).toHaveStyle({ width: `${width}px` });
  expect(screen.getByRole('separator')).toHaveAttribute('aria-valuenow', String(width));
}

describe('desktop sidebar resize', () => {
  it('starts at 260px with accessible bounds and a keyboard focus target', async () => {
    const { handle } = mount();
    expectWidth(260);
    expect(handle).toHaveAttribute('aria-orientation', 'vertical');
    expect(handle).toHaveAttribute('aria-valuemin', '200');
    expect(handle).toHaveAttribute('aria-valuemax', '420');
    await userEvent.setup().tab();
    expect(handle).toHaveFocus();
  });

  it('captures the pointer, resizes by its delta, clamps both limits and persists', () => {
    const { handle, capture, release } = mount();
    pointer(handle, 'pointerdown', { clientX: 264 });
    expect(capture).toHaveBeenCalledWith(1);
    expect(handle).toHaveAttribute('data-dragging', 'true');
    pointer(window, 'pointermove', { clientX: 312 });
    expectWidth(308);
    expect(localStorage.getItem(SIDEBAR_WIDTH_KEY)).toBe('308');
    pointer(window, 'pointermove', { clientX: 900 });
    expectWidth(420);
    pointer(window, 'pointermove', { clientX: -900 });
    expectWidth(200);
    pointer(window, 'pointerup');
    expect(release).toHaveBeenCalledWith(1);
    expect(handle).not.toHaveAttribute('data-dragging');
    pointer(window, 'pointermove', { clientX: 400 });
    expectWidth(200);
    pointer(handle, 'pointerdown', { clientX: 200 });
    pointer(window, 'pointermove', { clientX: 224 });
    expectWidth(224);
    pointer(window, 'pointerup');
  });

  it('ignores non-primary buttons, secondary pointers and unrelated pointer events', () => {
    const { handle, capture } = mount();
    pointer(handle, 'pointerdown', { button: 2 });
    pointer(handle, 'pointerdown', { isPrimary: false });
    expect(capture).not.toHaveBeenCalled();
    pointer(handle, 'pointerdown');
    pointer(handle, 'pointerdown', { pointerId: 2 });
    expect(capture).toHaveBeenCalledTimes(1);
    pointer(window, 'pointermove', { pointerId: 2, clientX: 400 });
    pointer(window, 'pointerup', { pointerId: 2 });
    pointer(window, 'pointercancel', { pointerId: 2 });
    expectWidth(260);
    expect(handle).toHaveAttribute('data-dragging', 'true');
    pointer(window, 'pointermove', { clientX: 280 });
    expectWidth(280);
    pointer(window, 'pointerup');
  });

  it('uses 16px arrow steps, Home for minimum and End for default, with bounded values', () => {
    const { handle } = mount();
    fireEvent.keyDown(handle, { key: 'ArrowRight' });
    expectWidth(276);
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    expectWidth(260);
    fireEvent.keyDown(handle, { key: 'Home' });
    fireEvent.keyDown(handle, { key: 'ArrowLeft' });
    expectWidth(200);
    for (let i = 0; i < 20; i++) fireEvent.keyDown(handle, { key: 'ArrowRight' });
    expectWidth(420);
    fireEvent.keyDown(handle, { key: 'End' });
    expectWidth(260);
    expect(localStorage.getItem(SIDEBAR_WIDTH_KEY)).toBe('260');
    const unrelated = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    fireEvent(handle, unrelated);
    expect(unrelated.defaultPrevented).toBe(false);
    expectWidth(260);
  });

  it('restores the default on double click and saves it for remount', () => {
    localStorage.setItem(SIDEBAR_WIDTH_KEY, '340');
    const view = mount();
    expectWidth(340);
    fireEvent.doubleClick(view.handle);
    expectWidth(260);
    expect(localStorage.getItem(SIDEBAR_WIDTH_KEY)).toBe('260');
    fireEvent.keyDown(view.handle, { key: 'ArrowRight' });
    view.unmount();
    mount();
    expectWidth(276);
  });

  it.each(['200', '260', '319.5', '420'])('loads a valid stored width: %s', value => {
    localStorage.setItem(SIDEBAR_WIDTH_KEY, value);
    mount();
    expectWidth(Number(value));
  });

  it.each(['{', '', 'null', 'true', '"320"', '{}', '[320]', '199', '421', '-1', '1e309', 'NaN'])
    ('falls back to default for corrupt, wrong-type or out-of-range data: %s', value => {
      localStorage.setItem(SIDEBAR_WIDTH_KEY, value);
      mount();
      expectWidth(260);
    });

  it.each(['read', 'write', 'accessor'])('continues resizing when storage fails: %s', failure => {
    if (failure === 'accessor') {
      vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('blocked'); });
    } else {
      vi.spyOn(Storage.prototype, failure === 'read' ? 'getItem' : 'setItem')
        .mockImplementation(() => { throw new Error('blocked'); });
    }
    const { handle } = mount();
    expectWidth(260);
    expect(() => fireEvent.keyDown(handle, { key: 'ArrowRight' })).not.toThrow();
    expectWidth(276);
    pointer(handle, 'pointerdown');
    pointer(window, 'pointermove', { clientX: 300 });
    expectWidth(316);
    pointer(window, 'pointerup');
  });

  it.each(['pointerup', 'pointercancel', 'lostpointercapture', 'blur'])
    ('restores selection/cursor and removes listeners when dragging ends via %s', type => {
      const { handle } = mount();
      const root = document.documentElement;
      const body = document.body;
      const previousRoot = root.style.cssText;
      const previousBody = body.style.cssText;
      try {
        root.style.setProperty('user-select', 'text', 'important');
        body.style.userSelect = 'auto';
        body.style.cursor = 'crosshair';
        pointer(handle, 'pointerdown');
        expect(root.style.userSelect).toBe('none');
        expect(body.style.userSelect).toBe('none');
        expect(body.style.cursor).toBe('col-resize');
        if (type === 'blur') fireEvent(window, new Event('blur'));
        else pointer(type === 'lostpointercapture' ? handle : window, type);
        expect(root.style.userSelect).toBe('text');
        expect(root.style.getPropertyPriority('user-select')).toBe('important');
        expect(body.style.userSelect).toBe('auto');
        expect(body.style.cursor).toBe('crosshair');
        expect(handle).not.toHaveAttribute('data-dragging');
        pointer(window, 'pointermove', { clientX: 400 });
        expectWidth(260);
      } finally {
        root.style.cssText = previousRoot;
        body.style.cssText = previousBody;
      }
    });

  it('cleans up all drag listeners, releases capture and restores selection on unmount', () => {
    const { handle, release, unmount } = mount();
    const add = vi.spyOn(window, 'addEventListener');
    const remove = vi.spyOn(window, 'removeEventListener');
    const handleAdd = vi.spyOn(handle, 'addEventListener');
    const handleRemove = vi.spyOn(handle, 'removeEventListener');
    const rootSelection = document.documentElement.style.userSelect;
    const bodySelection = document.body.style.userSelect;
    pointer(handle, 'pointerdown');
    const listeners = add.mock.calls.filter(([type]) => ['pointermove', 'pointerup', 'pointercancel', 'blur'].includes(type));
    expect(listeners).toHaveLength(4);
    const lostCapture = handleAdd.mock.calls.find(([type]) => type === 'lostpointercapture');
    expect(lostCapture).toBeDefined();
    unmount();
    for (const [type, listener] of listeners) expect(remove).toHaveBeenCalledWith(type, listener);
    expect(handleRemove).toHaveBeenCalledWith('lostpointercapture', lostCapture![1]);
    expect(release).toHaveBeenCalledWith(1);
    expect(document.documentElement.style.userSelect).toBe(rootSelection);
    expect(document.body.style.userSelect).toBe(bodySelection);
    const write = vi.spyOn(Storage.prototype, 'setItem');
    pointer(window, 'pointermove', { clientX: 400 });
    expect(write).not.toHaveBeenCalled();
  });

  it('does not disable selection or start resizing when pointer capture fails', () => {
    const { handle, capture } = mount();
    const selection = document.body.style.userSelect;
    capture.mockImplementation(() => { throw new Error('inactive pointer'); });
    expect(() => pointer(handle, 'pointerdown')).not.toThrow();
    expect(document.body.style.userSelect).toBe(selection);
    expect(handle).not.toHaveAttribute('data-dragging');
    pointer(window, 'pointermove', { clientX: 400 });
    expectWidth(260);
  });
});
