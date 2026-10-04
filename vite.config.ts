import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The dev server proxies /api and the WebSocket stream to the Python engine, so the dashboard only
// ever shows what the engine reports. Override the target with ENGINE_URL (default: local engine).
const engineUrl = process.env.ENGINE_URL ?? 'http://127.0.0.1:8080';

const proxy = {
  '/api': { target: engineUrl, changeOrigin: true },
  '/stream': { target: engineUrl.replace(/^http/, 'ws'), ws: true, changeOrigin: true },
};

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 3000,
    proxy,
  },
  preview: {
    host: '0.0.0.0',
    port: 3000,
    proxy,
  },
});
