import react from '@vitejs/plugin-react';
import { loadEnv, type Plugin } from 'vite';
import { readFileSync } from 'node:fs';
import theme from './tailwind.config';
import { defineConfig } from 'vitest/config';

// External SVG documents do not inherit page CSS. Inject the same semantic tokens
// for dev and build so the public asset contains no separately maintained colors.
function faviconPlugin(): Plugin {
  const { ink, canvas } = theme.theme.extend.colors;
  const source = readFileSync(new URL('./public/favicon.svg', import.meta.url), 'utf8')
    .replace('<svg ', `<svg style="--ink:${ink};--canvas:${canvas}" `);
  return {
    name: 'semantic-favicon',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (request.url?.split('?')[0] !== '/favicon.svg') return next();
        response.setHeader('Content-Type', 'image/svg+xml');
        response.end(source);
      });
    },
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'favicon.svg', source });
    },
  };
}

export default defineConfig(({ mode }) => {
  // Allow checks to run without reading local configuration or secrets.
  const skipEnvFiles = process.env.VITE_SKIP_ENV_FILES === '1';
  const env = skipEnvFiles ? process.env : loadEnv(mode, process.cwd(), 'VITE_');
  return {
    // Vite 5 expects a directory string. Keep its automatic search away from
    // local configuration by pointing at an intentionally absent directory.
    envDir: skipEnvFiles ? './.vite-no-env' : undefined,
    plugins: [react(), faviconPlugin()],
    server: {
      port: 3000,
      strictPort: true,
      proxy: {
        '/api': {
          target: env.VITE_PROXY_TARGET || 'http://127.0.0.1:8010',
          changeOrigin: true,
        },
      },
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      clearMocks: true,
      restoreMocks: true,
      unstubEnvs: true,
      unstubGlobals: true,
    },
  };
});
