---
title: 前端开发与构建
description: 说明两个 React 应用的分工、开发期代理、构建产物与生产托管方式。
---

# 前端开发与构建

仓库里有两个独立的 React 19 + Vite 应用，它们各自有依赖、锁文件与构建配置，**后端镜像里不包含任何前端产物**。构建与托管是独立的一步。

## 两个应用的分工

| 应用 | 目录 | dev 端口 | 面向 |
|---|---|---|---|
| 用户端 | `frontend/` | 5173 | 聊天、会话列表、模型选择、沙箱状态、账号设置、跨设备同步 |
| 管理后台 | `admin-frontend/` | 5174 | 概览（统计与待处理项）、提供商与模型、用户与角色 |

两者的技术差异：

| 维度 | `frontend/` | `admin-frontend/` |
|---|---|---|
| 路由 | 单页应用，内部状态切换 | `react-router`（`BrowserRouter`），路径为 `/login`、`/overview`、`/providers`、`/users` |
| 服务端状态 | 自研 store + hooks | TanStack Query（`QueryClientProvider`），queries 只读、写入走 mutations |
| 本地存储 | Dexie / IndexedDB | 不存业务数据 |
| 表格 | 自研列表 | TanStack Table |
| 表单 | 自研 | `react-hook-form` + `zod` |

管理后台的路由是真实的 URL 路径，刷新任意子页面都需要服务器把未知路径回落到 `index.html`（SPA fallback）。静态托管时这一条必须配。

两边的构建配置同构，所以下文的大部分规则对两个应用都成立：Vite + `@vitejs/plugin-react` + `@tailwindcss/vite`，别名 `@` 指向各自的 `src`。样式是 Tailwind CSS 4，**没有 `tailwind.config.js` 也没有 `postcss.config.js`**：设计令牌与应用样式写在各自的 `src/index.css` 里（`@import "tailwindcss"` 加 `@theme inline` 块），两份主题是复制关系而非共享文件——两个前端独立发版，样式各自演进。Node 版本要求写在各自的 `package.json`（`engines.node >= 20.11`）。

## 开发期：/api 代理与同源

两个 Vite 配置的 `server` 段只有端口不同，代理规则一字不差（`frontend/vite.config.js`、`admin-frontend/vite.config.js`）：

```js
server: {
  proxy: {
    '/api': 'http://127.0.0.1:8000',
  },
},
```

端口见上表（用户端 5173、管理后台 5174）：两个 dev server 同时开着是常态，端口必须分开。

代理的意义不只是转发：它让开发期的页面与接口**同源**，于是

- HttpOnly 会话 cookie 天然可用；
- 不需要 `allow_credentials=True`；
- 不需要 `allow_origins` 白名单；
- 不受 SameSite 在跨站场景下的限制。

跨域方案也能跑，但要求后端 CORS 与前端 fetch 三处同时对齐，任何一处漏掉都表现为「登录成功了，但下一个请求又是未登录」——比代理掩盖配置错误更难查。生产环境应把 `/api` 与静态资源收到同一域名（Nginx 反代），保持同源。

## VITE_API_BASE_URL

前端 API 基址由 Vite 在构建时内联 `import.meta.env.VITE_API_BASE_URL`（`frontend/src/lib/constants.ts`、`admin-frontend/src/lib/api.ts`）。用户端仓库里带一份值为空的 `frontend/.env`；管理后台只提交 `.env.example`，本地覆盖要自己新建 `admin-frontend/.env.local`（Vite 会加载，`.gitignore` 忽略）。

```text
# 值留空即可；要覆盖时写进 frontend/.env.local 或 admin-frontend/.env.local
VITE_API_BASE_URL=
```

留空 = 走相对路径 `/api`，配合 dev proxy（开发）或同域反代（生产）。只有在**前后端不同源**时才需要显式配置：

```text
VITE_API_BASE_URL=https://api.example.com
```

此时后端必须同步配置 `CORS_ALLOWED_ORIGINS`、`CORS_ALLOW_CREDENTIALS=true`，并把 cookie 设为 `AUTH_COOKIE_SAMESITE=none` + `AUTH_COOKIE_SECURE=true`（跨站部署的完整要求见[生产部署](deployment.md)）。

值末尾的 `/` 会被去掉，避免拼出 `//api/chat`——FastAPI 会把它当成另一个路径返回 404。两个应用各有一份实现，都是同一行 `.replace(/\/+$/, '')`。

## npm scripts

两个应用的脚本集完全一致：

| 命令 | 作用 |
|---|---|
| `npm run dev` | 启动 Vite dev server（含 HMR 与上面的 `/api` 代理） |
| `npm run build` | `tsc --noEmit` 类型检查 + `vite build` 产出静态文件 |
| `npm run typecheck` | 只做类型检查 |
| `npm run lint` | `oxlint` 静态检查 |
| `npm run test` | `vitest run` 跑单元测试 |
| `npm run test:watch` | vitest watch 模式 |
| `npm run preview` | 本地预览 `build` 的产物 |

测试是 node 环境的纯逻辑测试：`include` 只收 `src/**/*.test.{js,ts}`，`.tsx` 不被收集；依赖 IndexedDB 的模块在用例里 mock 掉 db，不依赖运行时上下文的组件用 `react-dom/server` 出静态 HTML。仓库不装 jsdom，也没有 E2E 配置。

## 构建产物

```bash
cd frontend && npm run build
cd admin-frontend && npm run build
```

`npm run build` 先跑 `tsc --noEmit`（类型错误直接中断构建），再由 Vite 产出静态文件。产物落在各应用的 `dist/`（各目录的 `.gitignore` 已忽略 `dist/`）：

- `dist/index.html` 与带 hash 的 JS/CSS 资源；
- `public/` 下的静态资源（图标等）被原样拷入。

构建过程不访问后端，可以在 CI 里独立完成。

用户端的版本号只有一个来源：`vite.config.js` 在构建时从 `package.json` 读 `version` 并注入 `__APP_VERSION__`，避免设置面板里出现第二份手写版本信息。

## 用户端的本地优先与同步

会话数据存在浏览器 IndexedDB（Dexie，库名 `BennettAgentDB`，表 `sessions` / `messages` / `syncMeta`），断网不会丢已同步的内容。同步引擎与后端配合：

- **push**：行级幂等 upsert，逐行接受或拒绝（单条 payload 超限的行被拒，不影响同批其余行）；
- **pull**：不透明游标长轮询，游标只在两路都拉完时才前进。

Dexie 的版本声明是**追加式历史**：已发布的版本声明不能回改，否则老客户端会走错升级路径。所以 `db/index.ts` 里保留了几个「当时看不出改动」的版本号。新增普通字段不需要新版本，只有索引变化才需要。

注意：**进入应用本身需要联网**——启动时要调 `/api/auth/me` 确认登录态，断网会停在登录页（目前没有 Service Worker）。

## 管理后台的数据层

TanStack Query 承担服务端状态：缓存、去重、失效、后台重取、失败重试。默认策略在 `admin-frontend/src/lib/query-client.ts`：

- `staleTime` 15 秒，窗口重新聚焦时重取；
- 401 / 403 / 404 不重试——重试一个权限错误只会拖慢错误呈现；
- mutations 一律不重试。

两条约定：**不做乐观更新**（管理动作失败必须让人看见真实状态），以及**写操作成功后只失效受影响的那几个 query key**。

## 生产：前端是独立的静态产物

生产前端**不在 compose 内**。`docker compose --profile app up -d` 只起 Postgres、Redis、RustFS 与后端 `app` 服务，不含任何前端进程。

原因是刻意的：把前端产物塞进后端进程会让「换一次前端」变成「重启一次 API」，也把后端镜像的体积、Node 工具链与前端发版节奏绑在一起。

生产托管有两种形态：

| 形态 | 做法 | 取舍 |
|---|---|---|
| 同域反代（推荐） | 静态服务器托管 `dist/`，Nginx 把 `/api` 转发到后端 | 保持同源，cookie 与 CORS 都不用额外配置 |
| 独立子域 | 前端挂 `app.example.com`，后端挂 `api.example.com` | 需要后端配 CORS、凭据模式与 `SameSite=None` + `Secure` |

静态托管必须满足两点：

1. SPA fallback：未知路径回落到 `index.html`（管理后台的 `react-router` 需要）；
2. `dist/` 里的资源是不可变文件，可以配长缓存；`index.html` 必须不缓存，否则发版后用户拿到旧的资源引用。

两个应用可以分别用两个子域，也可以放同一个静态服务器的两个路径下——后一种要在构建时给 Vite 传 `--base`：两份配置都没有预设 `base`，而资源引用默认是根路径 `/assets/...`。

## 相关源码

- `frontend/vite.config.js`
- `frontend/src/lib/constants.ts`
- `frontend/src/lib/api.ts`
- `frontend/src/lib/app-info.ts`
- `frontend/src/db/index.ts`
- `frontend/src/lib/sync/engine.ts`
- `admin-frontend/vite.config.js`
- `admin-frontend/src/lib/api.ts`
- `admin-frontend/src/lib/nav.ts`
- `admin-frontend/src/lib/query.tsx`
- `admin-frontend/src/lib/query-client.ts`
- `admin-frontend/src/App.tsx`
