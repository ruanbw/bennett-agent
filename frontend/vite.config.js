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
    // 不再代理 /api：前端通过 .env 的 VITE_API_BASE_URL 直连后端，代理会掩盖配置错误
    port: 5173,
  },
  test: {
    // 纯逻辑测试：不挂 jsdom。需要 IndexedDB / DOM 的模块（chat-persistence、
    // 各组件）不在这里测——它们要么靠 E2E，要么先把副作用抽到纯函数里。
    environment: 'node',
    include: ['src/**/*.test.{js,ts}'],
  },
})

