import path from 'path';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 后端地址可用 VITE_API_PROXY_TARGET 覆盖;启动脚本可通过 HDB_API_PORT 同步给前端代理。
const apiTarget = process.env.VITE_API_PROXY_TARGET || `http://127.0.0.1:${process.env.HDB_API_PORT || '8001'}`;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  base: '/',
  server: {
    port: 5174,
    // 允许通过 cloudflared 等隧道域名访问 dev server(公网演示用)
    allowedHosts: true,
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
