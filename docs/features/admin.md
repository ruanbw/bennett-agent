---
title: 管理后台
description: 独立的管理后台：提供商与模型 CRUD、预设与同步、设默认与启停、用户搜索分页、封禁解封与改角色，以及每条后台操作落到哪张表。
---

管理后台是独立的 React 应用（`admin-frontend/`，dev 端口 5174），与用户端共用同一套后端和同一个会话 cookie：在用户端登录过且角色够，打开后台就是已登录状态。它有自己的请求层（不做 401 全局广播），登录态只在启动探测与登录动作两个时机确定。

## 页面与路由

| 路由 | 页面 | 内容 |
|---|---|---|
| `/login` | 登录页 | 邮箱 + 密码；探测到已登录时带原路径回跳 |
| `/overview` | 概览 | 统计卡、需要处理的问题清单、提供商一览 |
| `/providers` | 模型提供商 | 提供商主表 + 展开行里的模型子表 |
| `/users` | 用户管理 | 搜索、筛选、分页、封禁解封、改角色 |
| `*` | 找不到 | 显式的「找不到」页，而不是静默跳回首页 |

`/login` 之外的整棵树包在角色守卫里。守卫有**四态**而不是三态：加载中 / 未登录 / 已登录 / **没有权限**。最后这态必须显式呈现——会话 cookie 与用户端共享，完全可能出现「已登录但角色不够」，把普通用户当成未登录会让他以为密码输错了，重输多少次都进不来。

侧边栏菜单与面包屑共用一张导航表（`lib/nav.ts`），不按角色过滤菜单：能进来就是 admin 以上，每个页面内部各自按角色收窄能力（例如只有 superadmin 能改角色）。菜单随角色变化会让后台导航跳来跳去更让人困惑。

## 权限边界

| 能力 | user | admin | superadmin |
|---|---|---|---|
| 进后台 | — | 是 | 是 |
| 查看提供商与模型 | — | 是 | 是 |
| 建 / 改 / 删提供商 | — | 是 | 是 |
| 同步模型、测试连接 | — | 是 | 是 |
| 启停模型、设默认 | — | 是 | 是 |
| 搜索与筛选用户 | — | 是 | 是 |
| 封禁 / 解封普通用户 | — | 是 | 是 |
| 封禁 / 解封同级管理员 | — | — | 是 |
| 改别人角色 | — | — | 是 |

前后端各有一道闸：前端按 `role` 决定按钮是否可用，后端按 `role` 决定请求是否放行。**前端的隐藏只是体验，后端的 403 才是边界**——绕过前端直接打接口，后端照样拦。

后端整组 `/api/admin` 挂在 `current_admin` 依赖上：未登录 401，角色不够 403。

## 后端接口清单

| 方法与路径 | 作用 | 权限 | 成功 | 典型失败 |
|---|---|---|---|---|
| `GET /api/admin/providers` | 列出提供商 | admin | 200 | — |
| `GET /api/admin/provider-presets` | 内置厂商预设 | admin | 200 | — |
| `POST /api/admin/providers` | 新建提供商 | admin | 201 | 400 地址不合法 / 400 未知预设 |
| `PATCH /api/admin/providers/{id}` | 更新提供商 | admin | 200 | 400 换地址未重填 key |
| `DELETE /api/admin/providers/{id}` | 删除提供商及其模型 | admin | 204 | 404 |
| `POST /api/admin/providers/{id}/test` | 测试连通性 | admin | 200（`ok=false` 也算成功） | — |
| `POST /api/admin/providers/{id}/sync` | 同步上游模型列表 | admin | 200 | 502 上游不可达或并发同步冲突 |
| `GET /api/admin/providers/{id}/models` | 列出该提供商的模型 | admin | 200 | 404 |
| `POST /api/admin/providers/{id}/models` | 手动添加模型 | admin | 201 | 409 该模型已存在 / 400 停用的不能设为默认 |
| `PATCH /api/admin/models/{id}` | 更新模型 | admin | 200 | 400 停用的不能设为默认 |
| `DELETE /api/admin/models/{id}` | 删除模型 | admin | 204 | 404 |
| `GET /api/admin/users` | 搜索 / 筛选 / 分页列出用户 | admin | 200 | — |
| `POST /api/admin/users/{id}/ban` | 封禁用户 | admin | 200 | 400 不能封自己 / 400 不能封同级 |
| `POST /api/admin/users/{id}/unban` | 解封用户 | admin | 200 | 400 不能解封同级 |
| `PATCH /api/admin/users/{id}/role` | 设置角色 | superadmin | 200 | 400 不能改自己 / 400 不能降级最后一个超管 |

几个刻意的状态码选择：

- 建/改提供商的**输入问题**返回 400 而不是 502。地址填错是输入问题；用 502 会让管理员以为网关挂了（只有 test / sync 的失败才是 502）。
- 「测试连接」失败返回 **200 + `ok=false`**，不是 5xx。测试失败是预期结果，界面要把原因显示在按钮旁边。
- 手动添加重复模型返回 409（唯一约束冲突），「模型已存在」是可恢复的冲突而不是内部错误。

## 提供商与模型的操作

提供商是「配置单位」，模型是它的从属数据：一次管理动作只针对一个提供商（同步、测试连接、编辑密钥）。所以主表是提供商，展开某一行才看到它下面的模型子表。允许多行同时展开——对齐两份提供商的模型清单是常见需求，而模型清单按 providerId 缓存，多展开也不会多请求。

模型子表默认平铺 8 行，超出折叠：一个 LiteLLM 网关常暴露上百个模型，全平铺只能靠滚动与肉眼扫描。子表带关键词过滤（模型 id 与展示名都匹配，大小写不敏感）。

单条模型的完整编辑项：展示名、上下文窗口、最大输出 tokens、是否支持思考。`supported_reasoning_levels` 与上游原始条目（`raw`）由同步写入，只读展示，不在编辑表单里手改。

## 用户搜索与分页

用户列表参数：`query`（按邮箱模糊搜索）、`status`（`all` / `active` / `banned`）、`limit`（1–200，默认 50）、`offset`。

两点细节：

- **筛选发生在计数之前**：`total` 必须是筛选后的总数，否则分页器会指向不存在的页。
- 搜索词里的 SQL `LIKE` 元字符（`%`、`_`）会被转义，且查询用 `escape="\"`——不转义时搜 `%` 会列出全部账号。

前端把页码与页大小当查询参数（用户量可以上万，不能一次拉全量再在前端切页），翻页等于换 query key。封掉最后一页的最后一个人时 offset 会越界，前端用 `clampUsersOffset` 夹回仍存在的最后一页——不夹的话会停在一个永远空着的第 2 页，看起来像「用户被删光了」。

## 后台改的东西落到哪张表、何时生效

这是后台最需要讲清的一件事：每个操作落到哪张表、生效时机是什么。

| 后台动作 | 落到哪 | 何时生效 | 备注 |
|---|---|---|---|
| 新建 / 修改提供商 | `model_provider` 一行 | **立即** | 改 Base URL 必须重填 API Key（否则新地址的主人会拿到旧 key） |
| 启停提供商 | `model_provider.enabled` | 立即 | 该提供商下所有模型从用户清单消失 |
| 删除提供商 | 删 `model_provider` + 其全部 `provider_model` | 立即 | 同时写入「注册表已初始化」标记，删光后重启不会用环境变量重新引导 |
| 同步模型 | `provider_model` 批量 upsert | 立即 | 只增不删；已存在模型的 `enabled` / `is_default` 不变 |
| 测试连接 | 不写库 | — | 只读上游 |
| 手动添加模型 | `provider_model` 一行 | 立即 | 与同步同表 |
| 启停模型 | `provider_model.enabled` | 立即 | 停用的模型自动取消默认标记 |
| 设默认 | `provider_model.is_default`（全局唯一） | 立即 | 停用的模型不能设为默认 |
| 封禁用户 | `user.is_active=false` + 删其全部 `access_token` | 立即 | 同时回收其沙箱租约；对方下一个请求即 401 |
| 解封用户 | `user.is_active=true` | 立即 | 对方需要重新登录（会话已在封禁时被删） |
| 改角色 | `user.role` + 派生的 `is_superuser` | 立即 | 成功后删该用户全部会话，对方需重新登录拿到新角色 |
| 删除模型 | `provider_model` 一行 | 立即 | — |

「立即」的含义：模型清单**不做任何缓存**。任一提供商行或注册表标记存在时，数据库即唯一权威，禁用立刻生效。不缓存是刻意的——缓存会让「管理员刚点的禁用」在若干秒内仍然可用，而这段时间足够发出若干次对话。

会话侧同样立即：`role` 每次请求都从库里读出，升降级后刷新页面即生效，不需要重新登录；改角色与封禁会额外删掉全部会话，让旧 token 立刻不可用。

## 密钥在界面上的样子

API Key 不回显。列表里只给掩码 `api_key_hint`（`sk-…xxxx`；长度不超过 8 的打成 `••••`）。编辑表单里的密钥输入框留空表示「保持不变」，显式勾选清除才写 `api_key=""`。

## 相关源码

- `app/api/admin.py` — 管理后台全部 HTTP 接口
- `app/services/model_service/provider_sync.py` — 提供商 / 模型的写用例与上游同步
- `app/services/model_service/provider_repo.py` — 行级读写
- `app/services/auth.py` — `current_admin` / `current_superadmin` 守卫
- `admin-frontend/src/App.tsx` — 路由表与四态登录守卫
- `admin-frontend/src/components/admin/ProvidersPanel.tsx` — 提供商与模型面板
- `admin-frontend/src/components/admin/UsersPanel.tsx` — 用户搜索、封禁与改角色
- `admin-frontend/src/lib/admin.ts` — 接口客户端与错误翻译
