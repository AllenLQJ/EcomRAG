import type { Config } from 'tailwindcss';

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    screens: { sm: '641px', md: '768px', lg: '1024px' },
    extend: {
      colors: {
        canvas: '#F6F6F4',
        card: '#FFFFFF',
        ink: '#16171A',
        muted: '#5C6068',
        spark: '#E07A3F',
        'body-muted': '#45484E',
        line: '#E4E4E0',
        outline: '#D6D6D1',
        accent: '#16171A',
        link: '#3346C8',
        cache: { bg: '#E9F4F0', text: '#0B5A4A', dot: '#0E7C66' },
        faq: { bg: '#EEF0FB', text: '#23339A', dot: '#3346C8' },
        general: { bg: '#F2F2EF', text: '#45484E', dot: '#8A8E95' },
        fallback: { bg: '#FBF1EA', text: '#8A3D12', dot: '#B4541A' },
        track: '#F0F0EC',
      },
      fontFamily: {
        serif: ['"Noto Serif SC"', '"Songti SC"', 'serif'],
        sans: ['"PingFang SC"', '"Noto Sans SC"', 'system-ui', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      fontSize: { title: ['22px', { lineHeight: '1.4', fontWeight: '600' }] },
      maxWidth: { chat: '760px', bubble: '520px' },
      borderRadius: {
        card: '12px', composer: '20px', send: '14px', avatar: '10px',
        user: '18px 18px 4px 18px',
      },
      boxShadow: { composer: '0 1px 2px rgb(22 23 26 / 0.04)' },
      keyframes: {
        'thinking-pulse': { '0%, 80%, 100%': { opacity: '0.25' }, '40%': { opacity: '1' } },
      },
      animation: { thinking: 'thinking-pulse 1.2s infinite' },
    },
  },
  plugins: [],
} satisfies Config;
