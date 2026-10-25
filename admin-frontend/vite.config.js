import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    port: 5174,
    // 代理 /api 到后端，让管理后台在开发期与后端**同源**（与 frontend/ 同一方案）。
    //
    // 认证走 HttpOnly cookie：同源下 cookie 天然可用，不需要 allow_credentials=True、
    // 不需要 allow_origins 白名单，也不受 SameSite 跨站限制。端口选 5174 是为了
    // 与用户端 dev server（5173）并存——两个服务同时开着是常态。
    //
    // 生产环境两种形态见 README：同域反代（首选，保持同源）或独立子域（需要后端
    // 配置 CORS 与 SameSite=None）。
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    // 与 frontend/ 一致：node 环境的纯逻辑测试（lib/admin 的草稿校验、错误翻译等），
    // 仓库不装 jsdom，不收 .tsx 测试。
    environment: 'node',
    include: ['src/**/*.test.{js,ts}'],
  },
})
