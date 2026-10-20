import { describe, expect, it, vi } from 'vitest';
import { brandSlogan, storeName, welcomeEnabled } from './config';

describe('brand configuration', () => {
  it('keeps the entry slogan alongside the configurable store name', () => {
    expect(storeName()).toBe('灵码');
    expect(brandSlogan()).toBe('买前问一问，买后有人管');
    vi.stubEnv('VITE_STORE_NAME', ' 云间 ');
    vi.stubEnv('VITE_BRAND_SLOGAN', ' 随时为你解答 ');
    expect(storeName()).toBe('云间');
    expect(brandSlogan()).toBe('随时为你解答');
  });
  it('falls back to the default slogan for a blank configuration', () => {
    vi.stubEnv('VITE_BRAND_SLOGAN', '  ');
    expect(brandSlogan()).toBe('买前问一问，买后有人管');
  });
});

describe('welcome animation configuration', () => {
  it.each(['production', 'development'])('defaults to enabled in %s', mode => {
    vi.stubEnv('MODE', mode);
    vi.stubEnv('VITE_WELCOME_ANIMATION', '');
    expect(welcomeEnabled()).toBe(true);
  });

  it.each(['0', 'false', 'off', ' FALSE ', 'Off'])('disables the animation with %s', value => {
    vi.stubEnv('MODE', 'production');
    vi.stubEnv('VITE_WELCOME_ANIMATION', value);
    expect(welcomeEnabled()).toBe(false);
  });

  it('defaults to disabled in test mode and permits an explicit opt-in', () => {
    vi.stubEnv('MODE', 'test');
    vi.stubEnv('VITE_WELCOME_ANIMATION', '');
    expect(welcomeEnabled()).toBe(false);
    vi.stubEnv('VITE_WELCOME_ANIMATION', '1');
    expect(welcomeEnabled()).toBe(true);
    vi.stubEnv('VITE_WELCOME_ANIMATION', 'off');
    expect(welcomeEnabled()).toBe(false);
  });
});
