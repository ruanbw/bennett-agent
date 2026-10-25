/// <reference types="vite/client" />

/**
 * Vite 环境变量类型。
 *
 * 没有这个声明，`import.meta.env` 在 tsc 下根本不存在（Vite 只在打包时注入，
 * 类型来自 vite/client），`npm run typecheck` 会直接报错。
 */
interface ImportMetaEnv {
  /** 后端 API 基址，见 admin-frontend/.env.example。 */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
