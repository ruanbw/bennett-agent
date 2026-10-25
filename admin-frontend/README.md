# Bennett 管理后台（admin-frontend）

独立的**管理后台**前端服务：模型提供商 / 模型价格 / 用户管理。与用户端
[`frontend/`](../frontend/) 是两个互不依赖的工程——没有共享包、没有共享构建，
UI 基件与主题 token 是**刻意的复制**（各自演进，改一边不牵动另一边）。两侧唯一
的交集是后端下发的**会话 cookie**：在用户端登录过且角色够，打开这里就是已登录状态。

技术栈与用户端一致：React 19 + TypeScript + Vite + Tailwind CSS v4。

## 开发

```bash
npm install
npm run dev        # http://127.0.0.1:5174，/api 已代理到 127.0.0.1:8000
```

需要后端在 8000 端口运行（见根目录 `main.py`）。先有一个管理员账号：

```bash
cd .. && uv run python scripts/seed_admin.py   # 播种初始超管，幂等
```

开发期与后端**同源**（vite proxy），cookie 天然可用，不需要配置 CORS。

## 构建

```bash
npm run build      # 产物在 dist/，交任意静态服务器托管
npm run preview    # 本地预览构建产物
```

## 生产部署

按后台与后端是否同源分两种形态：

**A. 同域路径（推荐，零 cookie/CORS 问题）**

后台部署在后端同域的子路径下，例如 `https://api.example.com/admin/`：

1. `vite.config.js` 加 `base: '/admin/'` 再构建（静态资源会带上前缀）；
2. 反代把 `/admin/` 指向 `dist/`，`/api/` 指向后端；
3. 用的是 history 路由，深度链接（如 `/admin/users` 刷新）需要静态服务器回退到
   入口页，nginx 即 `location /admin/ { try_files $uri /admin/index.html; }`；
   开发期 Vite 自动回退，不用配置。

同源之下 cookie、`credentials: 'include'`、SameSite 全部按默认行为工作，
后端不需要任何额外配置。

**B. 独立子域 / 独立域名**

例如后台在 `admin.example.com`、后端 API 在 `api.example.com`（或完全不同域）。
子域之间是 same-site，不同注册域则是 cross-site，按实际情况配置后端环境变量：

```bash
CORS_ALLOWED_ORIGINS=https://admin.example.com   # 加入后台的来源
CORS_ALLOW_CREDENTIALS=true                      # 跨源请求要带 cookie 时必开
AUTH_COOKIE_SAMESITE=none                        # 跨站部署时必须；同注册域子域可保持 lax
AUTH_COOKIE_SECURE=true                          # SameSite=None 的前置条件，生产强制
```

`settings.resolved_cookie_samesite()` 与启动校验会检查这组配置的合法性；
`OriginGuard` 的放行名单复用 `CORS_ALLOWED_ORIGINS`，加一处即可。

**注意**：`SameSite=None` 会把会话 cookie 暴露在跨站请求里，只在确有跨站需求时使用；
能用形态 A 就不要用 B。

## 登录与角色

- 会话与用户端共享（HttpOnly cookie，默认名 `bennett_session`），后台不提供注册；
  管理员由 `scripts/seed_admin.py` 播种或超管在「用户管理」里授予。
- 角色不够（已登录但是普通用户）会停在「没有管理后台权限」页，不会伪装成未登录。
- 权限的最终裁决在后端：`/api/admin/*` 整个路由挂 `current_admin` 门禁，
  改角色要求 `current_superadmin`；前端只负责把 403 渲染成可读的错误条。

## 设计要点（自用户端 README 随代码平移）

- **表单先本地拦错，再谈服务端 422。** 新增 / 编辑提供商时名称必填、接口地址
  必须是 `http://` 或 `https://` 开头（与后端一样区分大小写）；校验失败把焦点移到对应
  输入框并挂 `aria-invalid` / `aria-describedby`，而不是把 Pydantic 的
  `body.base_url: Value error…` 原样丢给管理员。API Key 输入框可显隐（默认密文，
  `aria-controls` 指向输入框）；编辑时留空 = 不修改、勾选「清除」才发空串，这条语义仍只在
  `buildProviderPayload` 一处实现。本地校验规则在 `lib/admin.ts:validateProviderDraft`，
  有 vitest 覆盖。同步结果也不写「新增 0 / 更新 0」这种无信息量文案：
  `lib/admin.ts:describeProviderSync` 会报上游数量、明说「没有变化」，并固定提醒
  **上游未返回的模型不会自动删除**（后端的只增不删策略）；同步失败的原因留在对应
  提供商卡片里，而不是飘到面板顶部。模型行还能编辑显示名 / 上下文窗口 / 最大输出 /
  推理标记：手动添加的模型 `context_window` 不再永远显示「窗口未知」；两个 token 上限
  可留空、填了必须是正整数（`validateModelDraft`，有 vitest），错误聚焦到对应输入框。
  模型显示名可能重复，所以开关 / 编辑 / 删除的 `aria-label` 都带 `model_id`。
  **模型一多要能过滤，默认也别全平铺。** 一个 LiteLLM 网关常暴露几十上百个模型，全部铺开
  时找某个具体型号只能靠滚动与肉眼扫描，而管理动作（启停 / 设默认 / 编辑）恰恰都针对某一个
  模型。卡片里因此有一个过滤框（ID 与展示名都匹配、大小写不敏感，`lib/admin.ts:filterAdminModels`
  有 vitest），默认只平铺 `VISIBLE_MODELS_COLLAPSED`（8）个，其余收进「展开全部（还有 N 个）」
  ——过滤时自动展开全部匹配项，并显示「匹配 1 / 18 个模型」。
- **用户列表刷新时不允许对旧行操作，页码也要自己夹回最后一页。** `loading` 由「本次请求
  key」与「最近一次完成的 key」推导，而不是在 effect 里同步 `setState(true)`（会触发
  级联渲染，React 的 lint 规则也会报）。刷新中禁封禁 / 角色 / 翻页，列表 `aria-busy`
  并降透明度，底部显示「第 a–b 个，共 N 个」与「正在刷新…」；封掉最后一页唯一用户后，
  `clampUsersOffset` 把越界 offset 夹回新的最后一页并重新拉取，不会停在一个永远空着的
  页。搜索无结果时提供「清除搜索」，不用回到顶部找清空按钮。

## 页面与路由

用的是 react-router（history 模式），菜单即路由：

| 路由 | 页面 | 菜单项 |
| --- | --- | --- |
| `/providers` | 提供商与模型维护 | 模型提供商 |
| `/prices` | 模型价格表 | 模型价格 |
| `/users` | 账号封禁与角色 | 用户管理 |

`/` 重定向到 `/providers`；未登录访问任意页面会跳 `/login` 并在登录后回跳原路径。

布局是标准的后台管理外壳（`src/layouts/AdminLayout.tsx`）：

- **左侧菜单可折叠**：折叠成图标栏，状态存 localStorage；md 以下折化为顶部横向菜单。
- **顶栏**放账号级操作（主题切换、当前账号、登出），菜单栏只管导航。
- **内容区通栏**，每个页面是「筛选表单 + 数据表格 + 行内操作」的经典形态：
  提供商页可展开行管理模型子表，价格页支持行内编辑，用户页带服务端搜索与分页。

## 目录结构

```
admin-frontend/
├── src/
│   ├── components/
│   │   ├── admin/          # 三个面板 + 两个编辑弹窗 + 开关（自 frontend/ 平移）
│   │   ├── ui/             # button / input / dialog / confirm-dialog（自 frontend/ 复制）
│   │   ├── ErrorBoundary.tsx
│   │   ├── LoginPage.tsx   # 后台自己的登录页（无注册，登录后回跳原路径）
│   │   └── ThemeSwitcher.tsx
│   ├── layouts/
│   │   └── AdminLayout.tsx # 左侧菜单 + 右侧内容的标准后台外壳
│   ├── context/            # 明暗主题（与用户端同源实现）
│   ├── hooks/
│   ├── lib/
│   │   ├── admin.ts        # /api/admin/* 客户端 + 草稿校验 + 错误翻译
│   │   ├── auth.ts         # 登录 / 登出 / 探测 / 角色判断
│   │   ├── api.ts          # 简化请求层（基址、cookie、超时、错误读取）
│   │   └── admin.test.ts
│   ├── App.tsx             # 路由表 + 登录态/角色守卫
│   └── main.tsx
├── vite.config.js          # dev 端口 5174，/api 代理到 8000
└── .env.example
```
