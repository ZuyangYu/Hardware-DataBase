import path from 'path';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Load .env.local as well as shell variables. Previously this config only
// read process.env, so changing frontend/.env.local had no effect and the
// dev page silently proxied to a stale API on port 8001. Keep this config as
// a plain object because vitest merges it with its own config.
const env = loadEnv(process.env.NODE_ENV === 'production' ? 'production' : 'development', process.cwd(), '');
const apiTarget =
  process.env.VITE_API_PROXY_TARGET ||
  env.VITE_API_PROXY_TARGET ||
  `http://127.0.0.1:${process.env.HDB_API_PORT || env.HDB_API_PORT || '8001'}`;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  base: '/',
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          'ui-vendor': ['radix-ui', 'sonner', 'lucide-react'],
          'markdown-vendor': ['react-markdown', 'remark-gfm'],
        },
      },
    },
  },
  server: {
    // Allow direct access from the host network as well as cloudflared 隧道域名(公网演示用)。
    // 保持域名白名单而非 allowedHosts: true,避免任意 Host 头访问 dev server。
    // 端口与 scripts/hdb.sh 的 FRONT_PORT 保持一致(5176)。
    host: '0.0.0.0',
    port: 5176,
    allowedHosts: ['.trycloudflare.com'],
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
      '/health': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
});
