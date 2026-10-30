---
title: 账号、认证与 RBAC
description: fastapi-users 的 HttpOnly Cookie 会话、三档 RBAC 边界、限流与封禁、超管播种脚本。
---

认证用 [fastapi-users](https://fastapi-users.info/) 的 Cookie 后端：会话状态存在 `accesstoken` 表（`sql/init.sql`）而不是 JWT，所以登出、改密、封禁、改角色都是立即生效——不需要等 token 自然过期。密码用 Argon2 哈希（`user.hashed_password`），会话 token 在库里只存 SHA-256 的 base64url 摘要（43 字符，正好填满 `token VARCHAR(43)`），库被读也无法直接回放。

## 路由

| 方法与路径 | 作用 | 关键细节 |
|---|---|---|
| `POST /api/auth/register` | 注册 | **JSON 体**（邮箱 + 密码，至少 8 位）；无论邮箱是否已存在都返回 202，避免变成邮箱枚举器；`REGISTRATION_ENABLED=false` 时 403；密码不合规 400 `REGISTER_INVALID_PASSWORD` |
| `POST /api/auth/login` | 登录 | **必须表单编码**（`username` 填邮箱 + `password`），发 JSON 直接 422；成功 204 并下发会话 cookie |
| `POST /api/auth/logout` | 登出 | 删除服务端会话行（不是「只让客户端忘掉」），幂等 |
| `GET /api/auth/me` | 当前登录用户 | 未登录 401；`role` 每次请求都从库里读出，升降级后刷新即生效 |
| `POST /api/auth/change-password` | 改密 | 必须验证当前密码；成功后该账号全部会话（含本次）立即失效；当前密码错返回 400 `CHANGE_PASSWORD_BAD_CREDENTIALS`，新旧相同 400 `CHANGE_PASSWORD_SAME` |

注册返回 202 而不是 201 是刻意的：框架自带的注册路由在邮箱已存在时返回 400，等于给了邮箱枚举接口。新建成功与已存在都返回同样的 202 + 空 body，前端随后用同一组凭据登录。改密的错误码统一用 400 而不是 401：401 会被前端的全局处理当成「会话过期」直接登出，而填错当前密码不该有这种后果。

没有忘记密码与邮箱验证路由：项目未配 SMTP，挂上却不发信比没有更糟。超管密码丢失的恢复通道是 `scripts/seed_admin.py --force-reset`。

## 会话与 Cookie

| 配置 | 默认值 | 说明 |
|---|---|---|
| `AUTH_COOKIE_NAME` | `bennett_session` | 会话 cookie 名 |
| `AUTH_COOKIE_SECURE` | `false` | 本地 http 调试必须保持 false，否则浏览器不回传 cookie（表现为「登录成功但下一个请求又未登录」）；生产必须 true |
| `AUTH_COOKIE_SAMESITE` | 自动 | CORS 带凭据时取 `none`，否则 `lax`；跨站部署写死 `lax` 会让 cookie 根本不被回传 |
| `SESSION_LIFETIME_SECONDS` | 30 天（下限 60） | 0 会让 fastapi-users 认为「不设过期」，与服务端语义矛盾，配置层直接拒绝 |

`HttpOnly` 写死为 true（`CookieTransport(cookie_httponly=True)`），没有配置项；cookie 的 `Max-Age` 直接取 `SESSION_LIFETIME_SECONDS`。启动期还有三道校验，因为它们的失败症状都是「登录 204，下一个请求 401」：`SameSite=None` 必须同时开 `AUTH_COOKIE_SECURE`；cookie 名必须是合法的 RFC 6265 token；`AUTH_COOKIE_NAME` 用 `__Host-` / `__Secure-` 前缀时要求 Secure（浏览器会静默丢弃这类 cookie）。

会话 token 入库前先摘要，读取时 Redis 做 cache-aside 热缓存：库是权威，撤销先删库再删缓存，缓存残留由短 TTL（`REDIS_SESSION_CACHE_TTL_SECONDS`，默认 60 秒，且不超过会话剩余寿命）兜住，所以撤销的传播延迟有界。缓存键是摘要而不是原文，Redis 被读也拿不到可回放的令牌。

## 三档角色

`app/roles.py` 是 RBAC 的唯一事实来源，`Role` 是 IntEnum，值的顺序就是权限顺序。

| 角色 | `role` 值 | 能力 | 边界 |
|---|---|---|---|
| 普通用户 | `user` | 只能对话 | 不能进管理后台 |
| 管理员 | `admin` | 管提供商与模型、封禁/解封普通用户、搜索用户列表 | **不能改任何人的角色**；不能封禁或解封同级与更高级账号 |
| 超级管理员 | `superadmin` | 管理员的全部能力，加上改别人的角色 | 不能改自己的角色；不能降级最后一个 superadmin |

鉴权只读 `user.role`，不读 `is_superuser`——后者只是 `role >= admin` 的派生镜像，由 `apply_role()` 单点写入。两个判据并存必然漂移。

非法角色值降级为 `user` 而不是抛错：`role` 是开放存储，手工改库写错一个 `'admn'` 若让鉴权依赖抛 500，结果是「这个账号连同站点一起不可用」，比「他退回普通用户」严重得多。

管理接口的守卫链是 `current_admin`（`role >= admin`）与 `current_superadmin`。未登录 401、权限不够 403，两者语义不同：前端靠这个区分「重新登录」与「找管理员」。

::: warning
没有任何接口能给自己提权：改角色接口拒绝 `user_id == 自己的 id`。
:::

## 限流

| 端点 | 维度 | 配置项 | 默认 |
|---|---|---|---|
| 注册、登录 | 来源 IP | `AUTH_RATE_LIMIT_PER_MINUTE` | 10 |
| 对话（`/api/chat`、`/api/chat/title` 共用一个桶） | 账号 | `CHAT_RATE_LIMIT_PER_MINUTE` | 30 |
| 跨设备同步（push + pull 共用一个桶） | 账号 | `SYNC_RATE_LIMIT_PER_MINUTE` | 120 |
| 改密码 | 账号 | `CHANGE_PASSWORD_RATE_LIMIT_PER_MINUTE` | 5 |

四项都是 `ge=0`，配成 0 表示关掉该桶（不是「一律拒绝」）；窗口固定 60 秒。登出不在限流范围内——它既不是暴力破解目标，被限流还会让人连登出都做不了。

认证按 IP、对话与同步按账号，是因为攻击面不同：暴力破解与批量注册的目标是常量账号，所以按来源 IP；对话消耗上游额度，同步只消耗本机数据库，两者分开才不会被互相拖累。认证端点还按路径末段分桶（`login` 与 `register` 各自一个），共用一桶时有人在同一 NAT 出口猛试密码会把正常注册一起打掉。

IP 取值只采信受信代理标头：直连对端必须落在 `TRUSTED_PROXY_IPS` 里才读 `X-Forwarded-For`，且从右往左跳过受信代理。IPv6 按 `/64` 归并，否则一个订阅的地址轮换就能绕过限流。限流响应带 `Retry-After`，Redis 不可用时按 `RATE_LIMIT_ON_REDIS_ERROR` 的三种模式降级。

## 封禁与解封

封禁 = `is_active = false` + 删除该用户的全部 `accesstoken` 行，并回收其沙箱租约。不能只改 `is_active`：留着旧会话的话，被改过的客户端仍能继续用已有租约烧算力。租约回收失败只告警——封禁本身已生效，剩下的机器最迟被空闲回收带走。

封禁与解封的等级校验完全对称：目标 `role` 不低于自己就拒绝（按 `role` 判，不按 `is_superuser`）。不对称就等于任意 admin 都能放回被超管封掉的 admin。封禁另外拒绝封自己；解封不需要这一条——被封着的人本来就登录不上。

## 改角色

`PATCH /api/admin/users/{id}/role` 只允许 superadmin 调用，且有四条额外约束：

- 不能改自己的角色。
- 不能改已封禁账号（先解封）。
- 不能降级最后一个 superadmin。用一条带子查询的 `UPDATE` 守卫完成计数与写入——先 count 再写会让并发的 A、B 互降都通过。
- 提交与当前角色相同的值视为幂等：直接返回 200，不写库也不踢会话。

角色变更成功后删除该用户的全部会话：旧 token 还能用等于「权限变更要等下次登录才生效」。

请求体只接受 `user` / `admin` 两个值（`Literal`，非法值在入口就 422）。`superadmin` 刻意不在其中：最高一档只能经运维通道授予，不能通过 HTTP 互相提权。

## 播种初始超级管理员

```bash
uv run python scripts/seed_admin.py --email admin@example.com
uv run python scripts/seed_admin.py --email admin@example.com --promote
uv run python scripts/seed_admin.py --email admin@example.com --force-reset
```

邮箱取自 `--email`，缺省时读 `SEED_ADMIN_EMAIL`。密码来源优先级：命令行 `--password` > 环境变量 `SEED_ADMIN_PASSWORD` > 交互输入（非 TTY 环境下没有可用密码就直接失败，不会卡在提示上）。

| 场景 | 默认行为 | 加了什么参数 |
|---|---|---|
| 账号不存在 | 创建为 superadmin | — |
| 已是 superadmin | 不做任何修改 | `--force-reset` 重置密码 |
| 已存在但角色较低 | **报错退出（exit code 2），不升权** | `--promote` 提升为 superadmin |

最后一条是刻意的：安静地把一个已存在的普通账号提升到最高权限，等于给「猜到某个已注册邮箱」留了一条后门。要升就说清楚。

密码重置会连带失效该账号的全部旧会话——密码重置的典型场景就是「密码可能泄露了」，旧 token 还能用等于只防住了新登录。密码交给 `UserManager` 处理，与线上登录用同一套 Argon2 配置与长度校验。

## AUTH_SECRET 的双重用途

`AUTH_SECRET` 同时承担两个职责：

1. fastapi-users 的重置密码令牌与邮箱验证令牌签名密钥。
2. 派生 Fernet 密钥，加密数据库里的提供商 API Key。

它**不参与会话**：轮换它不会让已登录用户掉线，但会让已加密的 API Key 无法解密，需要在管理后台重新填写（旧明文行不受影响）。

生产环境的启动守卫会拒绝弱密钥：不能是默认占位值、不能含 `change-me` / `replace-with` / `your-` / `example` / `placeholder` / `insecure` 之类的词、且长度至少 32 字符。生产还会强制 `AUTH_COOKIE_SECURE=true`，否则会话 token 会在明文 HTTP 上被链路上任意一跳截获。

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

## 会话过期清理

`DatabaseStrategy.read_token` 只按 `created_at` 过滤、从不删行，没有清理动作表会随登录次数无限增长。清理在登录成功后触发（`on_after_login`）：删除 `created_at` 早于 `SESSION_LIFETIME_SECONDS` 的会话行，并同步清掉 Redis 里的缓存项。清理失败只记警告——过期行堆积不该升级成「登录不可用」。

## 相关源码

- `app/roles.py` — 角色取值、等级顺序、字符串解析
- `app/services/auth.py` — Cookie 后端、会话策略、鉴权依赖
- `app/api/auth.py` — 注册 / 登录 / 登出 / me / 改密路由
- `app/api/rate_limit.py` — 滑动窗口限流与可信代理解析
- `app/db/models/user.py` — User / AccessToken 与 token 摘要
- `app/db/maintenance.py` — 过期会话清理、按用户踢下线
- `scripts/seed_admin.py` — 幂等播种超级管理员
