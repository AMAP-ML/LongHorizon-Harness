import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

const API = process.env.LH_HARNESS_WEB_API ?? 'http://127.0.0.1:8799';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    fs: { allow: [fileURLToPath(new URL('..', import.meta.url))] },
    proxy: { '/api': { target: API, changeOrigin: true, ws: true } },
  },
  // Build straight into the package so the wheel has a single source of truth
  // for the bundle; the directory is git-ignored and produced by CI.
  build: {
    outDir: fileURLToPath(new URL('../../src/lh_harness/_frontend/web/dist', import.meta.url)),
    emptyOutDir: true,
    sourcemap: false,
  },
});
