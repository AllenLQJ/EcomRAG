import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '../api/types';
import { MAX_PRODUCTS_BYTES } from '../api/products';
import { product, productAnswer } from '../test/products';
import { buildHistory } from './history';
import { CONVERSATIONS_KEY, loadMessages, resetStorageMemory, saveMessages } from './storage';

function turn(response = productAnswer([product()])): ChatMessage[] {
  return [
    { id: 'u', kind: 'user', text: '台灯规格' },
    { id: 'a', kind: 'assistant', question: '台灯规格', response, elapsedMs: 5 },
  ];
}

describe('product metadata in local history', () => {
  it('bounds cards and removes extra fields even for snapshots not received through the API', () => {
    const response = productAnswer(Array.from({ length: 4 }, (_, index) => ({ ...product(`lamp-${index}`),
      name: '灯'.repeat(160), highlights: ['灯'.repeat(256), '\u0000'.repeat(256), '第三条'],
      image_url: 'https://images.example.com/' + 'a'.repeat(1900), price: 999, stock: 10 })));
    saveMessages('s', turn(response));
    const raw = localStorage.getItem(CONVERSATIONS_KEY)!;
    expect(raw).not.toMatch(/"price"|"stock"|第三条/);
    resetStorageMemory();
    const loaded = loadMessages('s');
    const assistant = loaded[1];
    expect(assistant.kind).toBe('assistant');
    if (assistant.kind !== 'assistant') throw new Error('Missing persisted assistant');
    const products = assistant.response.products!;
    expect(products.length).toBeGreaterThan(0);
    expect(products.length).toBeLessThanOrEqual(3);
    expect(new TextEncoder().encode(JSON.stringify(products)).byteLength).toBeLessThanOrEqual(MAX_PRODUCTS_BYTES);
    expect(buildHistory(loaded)).toEqual([
      { role: 'user', content: '台灯规格' }, { role: 'assistant', content: response.answer },
    ]);
  });

  it('keeps old messages with no products and ignores malformed optional products without dropping the turn', () => {
    const messages = turn(productAnswer());
    saveMessages('s', messages);
    resetStorageMemory();
    expect(loadMessages('s')).toEqual(messages);
    const raw = JSON.parse(localStorage.getItem(CONVERSATIONS_KEY)!);
    raw[0].messages[1].response.products = [{ sku: 'broken' }];
    localStorage.setItem(CONVERSATIONS_KEY, JSON.stringify(raw));
    resetStorageMemory();
    expect(loadMessages('s')).toEqual(messages);
  });

  it('omits cards when existing text and sources exhaust the 32 KiB message budget', () => {
    const response = { ...productAnswer([product()]), answer: '灯'.repeat(8000),
      sources: Array.from({ length: 8 }, (_, index) => ({
        index: index + 1, doc_id: `doc-${index}`, title: '灯', snippet: '灯'.repeat(500), score: 1,
      })) };
    saveMessages('s', turn(response));
    const saved = loadMessages('s')[1];
    expect(saved).toMatchObject({ kind: 'assistant', response: { answer: response.answer, sources: response.sources } });
    if (saved.kind !== 'assistant') throw new Error('Missing persisted assistant');
    expect(saved.response).not.toHaveProperty('products');
  });
});
