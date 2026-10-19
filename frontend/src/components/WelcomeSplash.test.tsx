import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import { WelcomeSplash, WELCOME_KEY } from './WelcomeSplash';

const MOTION_QUERY = '(prefers-reduced-motion: reduce)';

function mockMotion(initial = false) {
  let reduced = initial;
  const listeners = new Set<() => void>();
  const add = vi.fn((_type: string, listener: () => void) => { listeners.add(listener); });
  const remove = vi.fn((_type: string, listener: () => void) => { listeners.delete(listener); });
  vi.stubGlobal('matchMedia', vi.fn((media: string) => ({
    media,
    get matches() { return media === MOTION_QUERY ? reduced : media === '(min-width: 1024px)'; },
    addEventListener: add,
    removeEventListener: remove,
  })));
  return { add, remove, listeners, change(next: boolean) { reduced = next; listeners.forEach(listener => listener()); } };
}

// jsdom may use Event instead of AnimationEvent; keep animationName explicit.
function animationEnd(element: HTMLElement, animationName = 'welcome-exit') {
  const event = new Event('animationend', { bubbles: true });
  Object.assign(event, { animationName });
  fireEvent(element, event);
}

function advance(ms: number) { act(() => { vi.advanceTimersByTime(ms); }); }
// 渲染后 React 自己会排一个调度定时器；先让它执行，再统计组件真正持有的定时器。
function timerCount() { advance(0); return vi.getTimerCount(); }

describe('WelcomeSplash', () => {
  beforeEach(() => { vi.useFakeTimers(); mockMotion(); });
  // 每个用例结束先卸载组件，再清掉残留定时器并还原真实计时器，避免上一个用例的假定时器基线串到下一个。
  afterEach(() => { cleanup(); vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks(); try { sessionStorage.clear(); } catch { /* 某些用例会故意让存储抛错 */ } });

  it('marks the first playback immediately and exits after 2300ms without stealing focus', () => {
    render(<><input aria-label="输入问题" autoFocus /><WelcomeSplash enabled /></>);
    const input = screen.getByRole('textbox');
    const dialog = screen.getByRole('dialog', { name: '欢迎来到灵码' });
    expect(dialog).toHaveAttribute('aria-modal', 'false');
    expect(dialog).toHaveAttribute('data-phase', 'playing');
    expect(input).toHaveFocus();
    expect(sessionStorage.getItem(WELCOME_KEY)).toBe('1');
    expect(screen.getByText('灵码 · 智能客服')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '欢迎来到灵码' })).toBeInTheDocument();
    expect(screen.getByText('让每一次提问，都有答案')).toBeInTheDocument();
    expect(screen.getByText('点击任意处跳过')).toBeInTheDocument();
    advance(2299);
    expect(dialog).toHaveAttribute('data-phase', 'playing');
    advance(1);
    expect(dialog).toHaveAttribute('data-phase', 'exiting');
    animationEnd(dialog);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(timerCount()).toBe(0);
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: '可以输入' } });
    expect(input).toHaveValue('可以输入');
  });

  it('uses the custom store name in the label, title and accessible name', () => {
    vi.stubEnv('VITE_STORE_NAME', '云间');
    render(<WelcomeSplash enabled />);
    expect(screen.getByRole('dialog', { name: '欢迎来到云间' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '欢迎来到云间' })).toBeInTheDocument();
    expect(screen.getByText('云间 · 智能客服')).toBeInTheDocument();
    expect(screen.queryByText(/灵码/)).not.toBeInTheDocument();
  });

  it('does not replay after a remount, even if the first playback was interrupted', () => {
    const first = render(<WelcomeSplash enabled />);
    expect(sessionStorage.getItem(WELCOME_KEY)).toBe('1');
    first.unmount();
    render(<WelcomeSplash enabled />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(timerCount()).toBe(0);
  });

  it('does not render when the session already contains the played marker', () => {
    sessionStorage.setItem(WELCOME_KEY, '1');
    render(<WelcomeSplash enabled />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(timerCount()).toBe(0);
  });

  it('starts exiting on click and uses the 700ms fallback if animationend never arrives', () => {
    render(<><input aria-label="输入问题" autoFocus /><WelcomeSplash enabled duration={10000} /></>);
    const dialog = screen.getByRole('dialog');
    // 假定时器下 RTL 的异步包装会等待真实时间，这里用同步点击，不影响断言目的。
    fireEvent.click(dialog);
    expect(dialog).toHaveAttribute('data-phase', 'exiting');
    expect(screen.getByRole('textbox')).toHaveFocus();
    advance(699);
    expect(dialog).toBeInTheDocument();
    advance(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(timerCount()).toBe(0);
  });

  it.each(['Escape', 'Enter', ' '])('skips with %s before the focused input handles the key', key => {
    const onKeyDown = vi.fn();
    render(<><input aria-label="输入问题" autoFocus onKeyDown={onKeyDown} /><WelcomeSplash enabled /></>);
    const input = screen.getByRole('textbox');
    const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
    fireEvent(input, event);
    expect(event.defaultPrevented).toBe(true);
    expect(onKeyDown).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('data-phase', 'exiting');
    // Keyboard input is available as soon as the exit begins.
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onKeyDown).toHaveBeenCalledTimes(1);
    expect(input).toHaveFocus();
    animationEnd(dialog);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('only accepts the overlay exit animation event, and supports a custom duration', () => {
    render(<WelcomeSplash enabled duration={100} />);
    const dialog = screen.getByRole('dialog');
    animationEnd(dialog);
    expect(dialog).toHaveAttribute('data-phase', 'playing');
    advance(100);
    animationEnd(screen.getByRole('heading'), 'welcome-exit');
    animationEnd(dialog, 'welcome-rise');
    expect(dialog).toBeInTheDocument();
    animationEnd(dialog);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(timerCount()).toBe(0);
  });

  it('skips and marks playback when reduced motion is already enabled', () => {
    mockMotion(true);
    render(<WelcomeSplash enabled />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(sessionStorage.getItem(WELCOME_KEY)).toBe('1');
    expect(timerCount()).toBe(0);
  });

  it('stops immediately if reduced motion is enabled during playback and does not restart', () => {
    const motion = mockMotion();
    const view = render(<WelcomeSplash enabled />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    act(() => motion.change(true));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(sessionStorage.getItem(WELCOME_KEY)).toBe('1');
    expect(timerCount()).toBe(0);
    act(() => motion.change(false));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    view.unmount();
    expect(motion.listeners.size).toBe(0);
  });

  it('defaults to disabled in test mode without marking a playback', () => {
    render(<WelcomeSplash />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(sessionStorage.getItem(WELCOME_KEY)).toBeNull();
    expect(timerCount()).toBe(0);
  });

  it.each(['0', 'false', 'off'])('stays disabled by configuration %s', value => {
    vi.stubEnv('MODE', 'production');
    vi.stubEnv('VITE_WELCOME_ANIMATION', value);
    render(<WelcomeSplash />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(sessionStorage.getItem(WELCOME_KEY)).toBeNull();
  });

  it('honors an explicit disabled prop and cancels an active playback when disabled', () => {
    const view = render(<WelcomeSplash enabled={false} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(sessionStorage.getItem(WELCOME_KEY)).toBeNull();
    view.rerender(<WelcomeSplash enabled />);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    view.rerender(<WelcomeSplash enabled={false} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(timerCount()).toBe(0);
  });

  it.each(['read', 'write', 'accessor'])('still plays and exits when sessionStorage fails: %s', failure => {
    if (failure === 'accessor') {
      vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => { throw new Error('blocked'); });
    } else {
      vi.spyOn(Storage.prototype, failure === 'read' ? 'getItem' : 'setItem')
        .mockImplementation(() => { throw new Error('blocked'); });
    }
    expect(() => render(<WelcomeSplash enabled duration={50} />)).not.toThrow();
    const dialog = screen.getByRole('dialog');
    advance(50);
    expect(dialog).toHaveAttribute('data-phase', 'exiting');
    advance(700);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it.each(['playing', 'exiting'])('clears timers and all listeners when unmounted while %s', phase => {
    const motion = mockMotion();
    const add = vi.spyOn(window, 'addEventListener');
    const remove = vi.spyOn(window, 'removeEventListener');
    const view = render(<WelcomeSplash enabled duration={10000} />);
    const keydown = add.mock.calls.find(([type]) => type === 'keydown');
    expect(keydown).toBeDefined();
    if (phase === 'exiting') fireEvent.click(screen.getByRole('dialog'));
    expect(timerCount()).toBe(1);
    view.unmount();
    expect(timerCount()).toBe(0);
    expect(remove).toHaveBeenCalledWith('keydown', keydown![1], true);
    expect(motion.listeners.size).toBe(0);
    expect(motion.remove).toHaveBeenCalledWith('change', motion.add.mock.calls[0][1]);
    advance(10000);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('keeps its playback timer through the StrictMode effect replay', () => {
    render(<StrictMode><WelcomeSplash enabled duration={100} /></StrictMode>);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('data-phase', 'playing');
    expect(timerCount()).toBe(1);
    advance(100);
    expect(dialog).toHaveAttribute('data-phase', 'exiting');
    animationEnd(dialog);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it.each(['0', '1'])('mounts from App in backend/mock mode %s and releases the composer on skip', mode => {
    vi.stubEnv('VITE_MOCK', mode);
    vi.stubEnv('VITE_WELCOME_ANIMATION', '1');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    const dialog = screen.getByRole('dialog', { name: '欢迎来到灵码' });
    const input = screen.getByRole('textbox', { name: '输入问题' });
    input.focus();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(dialog).toHaveAttribute('data-phase', 'exiting');
    animationEnd(dialog);
    fireEvent.change(input, { target: { value: '想了解商品' } });
    expect(input).toHaveFocus();
    expect(input).toHaveValue('想了解商品');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByText('历史仅保存在本浏览器')).toBeInTheDocument();
  });
});
