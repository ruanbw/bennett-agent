/// <reference types="vite/client" />

/**
 * Vite 环境变量类型。
 *
 * 没有这个声明，`import.meta.env` 在 tsc 下根本不存在（Vite 只在打包时注入，
 * 类型来自 vite/client），`npm run typecheck` 会直接报错。
 */
interface ImportMetaEnv {
  /** 后端 API 基址，见 frontend/.env。 */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

/**
 * 构建时注入的版本号（`vite.config.js` 的 `define`，来源是 package.json）。
 *
 * 定义在这里而不是从 package.json import：前端源码不应把整个 package.json 打进产物。
 */
declare const __APP_VERSION__: string
