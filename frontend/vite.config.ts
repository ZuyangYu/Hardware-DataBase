import path from 'path';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 后端地址可用 VITE_API_PROXY_TARGET 覆盖;启动脚本可通过 HDB_API_PORT 同步给前端代理。
const apiTarget = process.env.VITE_API_PROXY_TARGET || `http://127.0.0.1:${process.env.HDB_API_PORT || '8003'}`;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  base: '/',
  server: {
    // Allow direct access from the host network as well as cloudflared 隧道域名(公网演示用)。
    // 保持域名白名单而非 allowedHosts: true,避免任意 Host 头访问 dev server。
    // 端口与 scripts/hdb.sh 的 FRONT_PORT 保持一致(5175)。
    host: '0.0.0.0',
    port: 5175,
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
