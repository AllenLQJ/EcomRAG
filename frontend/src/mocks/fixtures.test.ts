import { describe, expect, it, vi } from 'vitest';
import { sendChat } from '../api/client';
import { fixtureFor } from './fixtures';

describe('Mock fixtures', () => {
  it.each([
    ['签收后有划痕，怎么退货', 'rag'], ['怎么申请退货', 'faq'],
    ['秒回', 'cache'], ['寄到火星', 'fallback'], ['你好', 'general'], ['其他问题', 'rag'],
  ])('matches %s to %s', (message, route) => {
    const response = fixtureFor({ session_id: 's', message, debug: false });
    expect(response.route).toBe(route);
    expect(response.is_mock).toBe(true);
  });

  it('returns three RAG citations and a trace only with debug', () => {
    const response = fixtureFor({ session_id: 's', message: '划痕', debug: true });
    expect(response.sources).toHaveLength(3);
    expect(response.answer).toContain('[1]');
    expect(response.answer).toContain('[1][2]');
    expect(response.answer).toContain('[3]');
    expect(response.trace).toHaveLength(6);
    expect(response.trace).toContainEqual({ stage: 'retrieve', ms: 120, candidates: 10 });
    expect(response.trace?.find(item => item.stage === 'generate')?.detail).toContain('本地模拟耗时');
    expect(fixtureFor({ session_id: 's', message: '划痕', debug: false }).trace).toBeNull();
  });

  it.each([['限流', 42901], ['超时', 50401]] as const)('simulates %s after a short delay without fetch', async (message, code) => {
    vi.useFakeTimers();
    vi.stubEnv('VITE_MOCK', '1');
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const assertion = expect(sendChat({ session_id: 's', message, debug: false })).rejects.toMatchObject({ code });
    await vi.advanceTimersByTimeAsync(1500);
    await assertion;
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('delays a successful local response within 600–1500ms', async () => {
    vi.useFakeTimers();
    vi.stubEnv('VITE_MOCK', '1');
    vi.spyOn(Math, 'random').mockReturnValue(0);
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const done = vi.fn();
    const promise = sendChat({ session_id: 's', message: '你好', debug: false }).then(done);
    await vi.advanceTimersByTimeAsync(599);
    expect(done).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await promise;
    expect(done).toHaveBeenCalledWith(expect.objectContaining({ is_mock: true, route: 'general' }));
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it('returns a cache hit and an optional cache trace for 秒回', () => {
    const request = { session_id: 's', message: '试试秒回', debug: true };
    const response = fixtureFor(request);
    expect(response.route).toBe('cache');
    expect(response.sources[0].doc_id).toBe('faq:12');
    expect(response.trace).toEqual([{ stage: 'cache', ms: 2, hit: true }]);
    expect(fixtureFor({ ...request, debug: false }).trace).toBeNull();
  });

});
