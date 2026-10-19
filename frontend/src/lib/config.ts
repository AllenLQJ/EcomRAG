export function humanSupportUrl(): string | undefined {
  const value = import.meta.env.VITE_HUMAN_SUPPORT_URL?.trim();
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
}

export function storeName(): string {
  return import.meta.env.VITE_STORE_NAME?.trim() || '灵码';
}

export function welcomeEnabled(): boolean {
  const value = import.meta.env.VITE_WELCOME_ANIMATION?.trim().toLowerCase();
  if (value === '0' || value === 'false' || value === 'off') return false;
  return import.meta.env.MODE !== 'test' || Boolean(value);
}

export type LogoVariant = 'a-spark' | 'b-seal' | 'c-dialog';
const LOGOS: readonly LogoVariant[] = ['a-spark', 'b-seal', 'c-dialog'];

export function logoVariant(): LogoVariant {
  const value = import.meta.env.VITE_LOGO?.trim();
  return LOGOS.find((item) => item === value) ?? 'b-seal';
}
