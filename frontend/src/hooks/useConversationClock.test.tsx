import { act, fireEvent, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import * as time from '../lib/conversationTime';
import { useConversationClock } from './useConversationClock';

const start = new Date(2026, 9, 18, 12).getTime();

describe('conversation calendar clock', () => {
  it('updates relative labels every 60 seconds and removes both timers on unmount', () => {
    vi.useFakeTimers();
    vi.setSystemTime(start);
    const { result, unmount } = renderHook(() => useConversationClock());
    act(() => vi.advanceTimersByTime(59_999));
    expect(result.current).toBe(start);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe(start + 60_000);
    expect(vi.getTimerCount()).toBe(2);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
  it('recomputes the local date boundary after a timezone change on focus, without leaving the old timer', () => {
    vi.useFakeTimers();
    vi.setSystemTime(start);
    const boundary = vi.spyOn(time, 'nextLocalMidnight').mockImplementation(now => now + 12 * 3_600_000);
    const { result, unmount } = renderHook(() => useConversationClock());
    act(() => vi.advanceTimersByTime(10_000));
    // The new local timezone puts midnight 10 seconds from the focus event.
    boundary.mockReturnValueOnce(start + 20_000);
    fireEvent.focus(window);
    expect(result.current).toBe(start + 10_000);
    expect(boundary).toHaveBeenLastCalledWith(start + 10_000);
    expect(vi.getTimerCount()).toBe(2);
    act(() => vi.advanceTimersByTime(9_999));
    expect(result.current).toBe(start + 10_000);
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe(start + 20_000);
    expect(boundary).toHaveBeenLastCalledWith(start + 20_000);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
  it('refreshes after visibility/clock changes and detaches its event handlers', () => {
    vi.useFakeTimers();
    vi.setSystemTime(start);
    const boundary = vi.spyOn(time, 'nextLocalMidnight');
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    const { result, unmount } = renderHook(() => useConversationClock());
    const tomorrow = start + 86_400_000;
    vi.setSystemTime(tomorrow);
    fireEvent(document, new Event('visibilitychange'));
    expect(result.current).toBe(start);
    visibility.mockReturnValue('visible');
    fireEvent(document, new Event('visibilitychange'));
    expect(result.current).toBe(tomorrow);
    expect(boundary).toHaveBeenLastCalledWith(tomorrow);
    unmount();
    boundary.mockClear();
    fireEvent.focus(window);
    fireEvent(document, new Event('visibilitychange'));
    expect(boundary).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
