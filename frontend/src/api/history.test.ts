import { describe, expect, it, vi } from 'vitest';
import { sendChatStream } from './client';
import type { ChatRequest } from './types';
import * as fixtures from '../mocks/fixtures';

const request: ChatRequest = { session_id: 'context', message: '继续', debug: false, history: [
  { role: 'user', content: '先前的问题' }, { role: 'assistant', content: '先前的回答' },
] };

describe('history in Mock transports', () => {
  it.each(['0', '1'])('passes the same history into Mock with VITE_STREAM=%s', async stream => {
    vi.stubEnv('VITE_MOCK', '1');
    vi.stubEnv('VITE_STREAM', stream);
    const response = fixtures.fixtureFor(request);
    const json = vi.spyOn(fixtures, 'mockChat').mockResolvedValue(response);
    const sse = vi.spyOn(fixtures, 'mockChatStream').mockResolvedValue(response);
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    expect(await sendChatStream(request, vi.fn())).toEqual(response);
    const transport = stream === '1' ? sse : json;
    expect(transport.mock.calls[0][0]).toEqual(request);
    expect(transport.mock.calls[0][0].history).toEqual(request.history);
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it.each(['0', '1'])('actual Mock accepts history without modifying it (stream=%s)', async stream => {
    vi.useFakeTimers();
    vi.stubEnv('VITE_MOCK', '1');
    vi.stubEnv('VITE_STREAM', stream);
    const before = JSON.stringify(request);
    const pending = sendChatStream(request, vi.fn());
    await vi.runAllTimersAsync();
    expect((await pending).is_mock).toBe(true);
    expect(JSON.stringify(request)).toBe(before);
  });
});
