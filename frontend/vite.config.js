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
    port: 5173,
    // 代理 /api 到后端，让前端在开发期与后端**同源**。
    //
    // 这里推翻了此前「不代理」的决定（原注释：代理会掩盖配置错误）。改判的理由是
    // 认证：会话走 HttpOnly cookie，而同源下 cookie 天然可用——不需要
    // allow_credentials=True、不需要 allow_origins 白名单、不需要 credentials: 'include'，
    // 也不受 SameSite 在跨站场景下的限制。
    //
    // 跨域方案也能跑（localhost:5173 与 :8000 端口不同但同 site，SameSite=Lax 仍会带上
    // cookie），但它要求后端 CORS 与前端 fetch 三处同时对齐，任何一处漏掉都表现为
    // 「登录成功了，但下一个请求又是未登录」——这比原先担心的「配置错误被掩盖」更难查。
    //
    // 生产环境同样应把 /api 与静态资源收到同一域名（Nginx 反代），保持同源。
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    // 纯逻辑测试：不挂 jsdom。需要 IndexedDB / DOM 的模块（chat-persistence、
    // 各组件）不在这里测——它们要么靠 E2E，要么先把副作用抽到纯函数里。
    environment: 'node',
    include: ['src/**/*.test.{js,ts}'],
  },
})

