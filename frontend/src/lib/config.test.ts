import { describe, expect, it, vi } from 'vitest';
import { welcomeEnabled } from './config';

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
