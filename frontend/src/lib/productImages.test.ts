import { describe, expect, it, vi } from 'vitest';
import { productImageUrl } from './productImages';

describe('product image URLs', () => {
  it('defaults an unset image root to same-origin /images, independent of API base and page path', () => {
    vi.stubEnv('VITE_PRODUCT_IMAGE_BASE_URL', undefined);
    vi.stubEnv('VITE_API_BASE', 'https://api.example.test');
    window.history.replaceState({}, '', '/chat/session');
    expect(productImageUrl('lamp/main.webp', 'lamp')).toBe('/images/lamp/main.webp');
  });
  it.each(['https://images.example.com/products/lamp/main.webp', 'http://images.example.com/lamp/main.svg'])('preserves a complete HTTP(S) URL: %s', url => {
    vi.stubEnv('VITE_PRODUCT_IMAGE_BASE_URL', '');
    expect(productImageUrl(url, 'lamp')).toBe(url);
  });

  it.each(['https://images.example.com/products', 'https://images.example.com/products///'])('resolves paths against the dedicated image root: %s', root => {
    vi.stubEnv('VITE_API_BASE', 'https://chat.example.com/api');
    vi.stubEnv('VITE_PRODUCT_IMAGE_BASE_URL', root);
    expect(productImageUrl('lamp/main.webp', 'lamp')).toBe('https://images.example.com/products/lamp/main.webp');
  });

  it('supports a same-origin image root and the backend SVG placeholder path', () => {
    vi.stubEnv('VITE_PRODUCT_IMAGE_BASE_URL', '/products/');
    window.history.replaceState({}, '', '/chat/session');
    expect(productImageUrl('lamp/main.svg', 'lamp')).toBe('/products/lamp/main.svg');
  });

  it('uses the built-in placeholder when a relative path has no configured image host', () => {
    vi.stubEnv('VITE_PRODUCT_IMAGE_BASE_URL', '');
    expect(productImageUrl('lamp/main.webp', 'lamp')).toBeUndefined();
  });

  it.each(['javascript:alert(1)', 'data:image/svg+xml,anything', '//other.example/lamp/main.webp', '../lamp/main.webp',
    'other/main.webp', '/lamp/main.webp', 'lamp/main.webp?x=1', 'https://user:secret@images.example/main.webp', '', null])('rejects invalid or unsafe paths: %j', url => {
    vi.stubEnv('VITE_PRODUCT_IMAGE_BASE_URL', '/products');
    expect(productImageUrl(url, 'lamp')).toBeUndefined();
  });

  it.each(['//images.example', 'products', 'data:abc', 'https://user:secret@images.example',
    'https://images.example/products?x=1', 'https://images.example/products#x'])('rejects an invalid image root: %s', root => {
    vi.stubEnv('VITE_PRODUCT_IMAGE_BASE_URL', root);
    expect(productImageUrl('lamp/main.webp', 'lamp')).toBeUndefined();
  });
});
