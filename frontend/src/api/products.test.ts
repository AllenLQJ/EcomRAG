import { describe, expect, it, vi } from 'vitest';
import { product, productAnswer } from '../test/products';
import { controlledSSE } from '../test/streams';
import { sendChat, sendChatStream } from './client';
import { MAX_PRODUCTS_BYTES, normalizeProducts } from './products';

const request = { session_id: 's', message: '台灯规格', debug: false };
const jsonResponse = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });

describe('optional product response metadata', () => {
  it.each([1, 2, 3])('preserves %i products from JSON', async count => {
    const response = productAnswer(Array.from({ length: count }, (_, index) => product(`lamp-${index}`)));
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(response)));
    expect(await sendChat(request)).toEqual(response);
  });

  it('preserves products on the SSE done event and the returned result', async () => {
    const stream = controlledSSE();
    const response = productAnswer([product()]);
    stream.event({ event: 'delta', data: { text: '台灯' } });
    stream.event({ event: 'done', data: response });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(stream.response));
    const emit = vi.fn();
    expect(await sendChatStream(request, emit)).toEqual(response);
    expect(emit.mock.calls).toEqual([
      [{ event: 'delta', data: { text: '台灯' } }], [{ event: 'done', data: response }],
    ]);
  });

  it.each([undefined, null, [], 'bad', [{ sku: 'broken' }]])('keeps the answer usable without valid products: %j', async products => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async () => jsonResponse({ ...productAnswer(), products })));
    expect(await sendChat(request)).toEqual(productAnswer());
  });

  it('preserves products through JSON fallback and when streaming is disabled', async () => {
    const response = productAnswer([product()]);
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response('missing', { status: 404 }))
      .mockResolvedValueOnce(jsonResponse(response));
    vi.stubGlobal('fetch', fetchMock);
    expect(await sendChatStream(request, vi.fn())).toEqual(response);
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual(['/api/v1/chat/stream', '/api/v1/chat']);
    vi.stubEnv('VITE_STREAM', '0');
    fetchMock.mockResolvedValueOnce(jsonResponse(response));
    expect(await sendChatStream(request, vi.fn())).toEqual(response);
  });

  it('drops unknown commercial fields, invalid entries and duplicate SKUs', () => {
    expect(normalizeProducts([{ ...product(), price: 199, stock: 10 }, product(), product('lamp-2')]))
      .toEqual([product(), product('lamp-2')]);
    expect(normalizeProducts([{ ...product(), name: 'x'.repeat(161) }])).toBeUndefined();
    expect(normalizeProducts([{ ...product(), highlights: [] }])).toBeUndefined();
    expect(normalizeProducts([{ ...product(), image_url: { unexpected: true } }]))
      .toEqual([{ ...product(), image_url: null }]);
  });

  it('bounds UTF-8 metadata including JSON escaping before storage', () => {
    const products = Array.from({ length: 6 }, (_, index) => ({ ...product(`lamp-${index}`),
      name: '灯'.repeat(160), category: '灯'.repeat(64), highlights: ['灯'.repeat(256), '\u0000'.repeat(256)],
      image_url: 'https://images.example.com/' + 'a'.repeat(1900) }));
    const bounded = normalizeProducts(products)!;
    expect(bounded.length).toBeGreaterThan(0);
    expect(bounded.length).toBeLessThan(6);
    expect(new TextEncoder().encode(JSON.stringify(bounded)).byteLength).toBeLessThanOrEqual(MAX_PRODUCTS_BYTES);
  });
});
