---
title: 数据模型
description: 十张表的职责、关键列、索引与状态机，以及 init.sql、漂移检测与删除语义。
---

# 数据模型

数据层只有一个事实来源：`sql/init.sql`。整段脚本幂等，应用启动时在事务里执行一次；不维护增量迁移。改表就直接改脚本，然后重建开发库：

```bash
docker compose down -v && docker compose up -d postgres
```

`CREATE TABLE IF NOT EXISTS` 对既有表不会补列，所以启动时还会逐列比对 SQLAlchemy 模型与真实库，缺列就拒绝启动并打印可复制的 `ALTER TABLE`（`app/db/schema.py:verify_schema_at_head`）。测试零基础设施、不连数据库（`tests/conftest.py`），抓不到这类漂移；这道守卫是给升级中的生产准备的。

## 表关系

```text
model_provider 1 ── n provider_model

"user" 1 ── n accesstoken
"user" 1 ── 1 sync_counter
"user" 1 ── n conversation 1 ── n message
"user" 1 ── n sandbox_lease 1 ── n sandbox_command

app_setting      独立键值表（模型注册表初始化标记）
```

用户数据表都以 `user_id` 作为归属判据；`app_setting`、`model_provider`、`provider_model` 是全局配置，没有归属列。`conversation.id`、`message.id` 由客户端生成，服务端只用 `user_id` 判定能不能读写。`message.user_id` 是刻意的冗余：拉取时不必 join `conversation`。

## 表一览

| 表 | 一行是什么 | 关键约束 |
|---|---|---|
| `app_setting` | 极简键值配置 | `key` 主键 |
| `model_provider` | 一个 OpenAI 兼容端点 + 凭据 | `slug` 唯一；`api_key` 加密落库 |
| `provider_model` | 某提供商下的一个模型 | `(provider_id, model_id)` 唯一；删提供商级联删模型 |
| `user` | fastapi-users 用户 + `role` | `email` 唯一；`role` 是唯一权限判据 |
| `accesstoken` | 一条服务端会话 | `token` 主键（存 SHA-256 摘要）；`created_at` 索引供过期清理；删用户级联 |
| `sync_counter` | 每用户单调递增版本号 | `user_id` 主键 |
| `conversation` | 会话的服务端镜像 | `id` 客户端生成；`(user_id, version)` 索引 |
| `message` | 消息的服务端镜像 | `(conversation_id, seq)` 唯一；`payload` 整包 JSONB |
| `sandbox_lease` | 一台沙箱机器的租约 | 活动状态下 `user_id` 部分唯一；`(state, hard_expires_at)` 索引 |
| `sandbox_command` | 沙箱命令审计 | `(lease_id, seq)` 索引；删租约级联 |

## 模型提供商与模型

`model_provider.slug` 参与对外路由键 `{slug}/{model_id}`，创建后不随改名变化，否则用户已保存的模型选择会被悄悄改掉。`api_key` 用 `EncryptedText` 落库（`AUTH_SECRET` 派生密钥），接口只回掩码；`auth_style` 决定 Key 放进 `Authorization: Bearer`、`x-api-key`、`x-goog-api-key` 还是不带；`extra_headers` 存 Anthropic 这类厂商的固定请求头，改预设不会悄悄改已有行。

`provider_model` 的 `model_id` 是发给上游的名字，不参与 URL；`enabled` 控制对用户是否可见；`is_default` 决定不传 `model` 时用哪个；`supported_reasoning_levels` 是 JSONB 数组，用来校验请求的思考等级；`raw` 保存上游最近一次同步返回的原始条目，手动添加的模型为 `NULL`。

`app_setting` 目前只存一个标记 `model_registry_initialized`。判定见 `registry_configured`：标记存在，或 `model_provider` 有任何一行（后者兼容标记上线前建好的库）。只看提供商行数分不清「全新部署」与「管理员删光了提供商」——前者该退回环境变量网关，后者该没有模型。标记一经写入永不清除，所以删光提供商后重启不会再次引导。

## 用户与会话

`user` 是 fastapi-users 的标准表加一列 `role`。`role` 取值 `user` / `admin` / `superadmin`，是唯一权限判据；`is_superuser` 只是它的派生镜像，由 `apply_role()` 单点写入，鉴权不读它。非法角色字符串在解析时降级为 `user`，不让一个拼错的值把整个账号鉴权打成 500。

`accesstoken.token` 存的是 cookie 原文的 SHA-256 base64url 摘要（43 字符，`hash_access_token`），不是明文；库被读也无法直接回放会话。登出、改密、封禁都是删行，立即生效。Redis 里有一层 cache-aside 热缓存，键同样是摘要，TTL 默认 60 秒；撤销先删库再删缓存，缓存残留最多让撤销晚一个 TTL。

表上没有过期列：过期由 `created_at + SESSION_LIFETIME_SECONDS`（默认 30 天）推导，`DatabaseStrategy.read_token` 只拿它当过滤条件、从不删行。所以清理是显式动作——启动时与每次登录成功后各删一次过期行，走 `ix_accesstoken_created_at`；改密则走 `on_after_update` → `delete_user_access_tokens`，按 `user_id` 删掉该账号的全部会话。

## 同步：版本号与消息顺序

`sync_counter` 每用户一行，`version` 由一条 `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` 发号；行锁保证并发 push 不会拿到同一个号。发号必须与业务写入同事务，否则会出现「版本前进、行还没写」的空洞，客户端游标一过就永久丢数据。

`conversation` 和 `message` 是浏览器 Dexie 的服务端镜像：

- `conversation.id` 由客户端生成（`sess_<...>`，见 `frontend/src/lib/constants.ts:newId`），服务端不发行主键；否则重试会变成多一行。
- `conversation.seq_counter` 是每个会话独立的消息顺序发号器；`message.seq` 只在插入时赋值、之后不变，是同一会话内跨设备顺序的唯一依据。
- `version` 是拉取游标的来源。任何一行变化都取新号；`(user_id, version)` 索引让增量拉取直接命中。
- `client_rev` 是客户端修订号，服务端据此拒绝「旧内容覆盖新内容」的重放。
- `deleted` 是墓碑，不是硬删除：删会话只改这一位（硬删会话行会让另一台设备把它推回来），该会话的 `message` 行则直接清掉——墓碑是删除信号，消息没有保留价值。
- `created_at` / `updated_at` 是客户端毫秒时间戳，只用于展示排序，不能当同步判据。

## 沙箱租约与命令审计

`sandbox_lease` 是长生命周期资源，必须落库，不能只放在进程内存：

| 列 | 作用 |
|---|---|
| `session_id` | 最后使用这台机器的会话标识 `sess_<...>`；只用于归因，不参与寻址（身份来自服务端注入的作用域） |
| `provider` / `provider_ref` | 换后端时旧租约该找谁回收；`provider_ref` 只在服务端流动，绝不进工具参数 |
| `template` / `template_version` | 记录用户当时跑在哪个镜像版本，模板升级后旧机器行为不同，排查要能对上 |
| `state` | `provisioning` / `ready` / `paused` / `destroying` / `destroyed` / `failed` / `destroy_failed` |
| `last_activity` | 服务端观察到的最后一次 exec / 文件读写；关掉页面不影响在跑的长任务 |
| `hard_expires_at` | 绝对上限（默认 4 小时）；只靠空闲回收的话，挂着不动的标签页可以永远占机 |
| `destroying_since` | 进入 `destroying` 的时刻；区分「正在销毁」与「销毁被中断」，两者的处置相反 |
| `destroy_reason` | `idle` / `hard_ttl` / `session_deleted` / `user_banned` / `user_reset` / `error` |
| `artifact_uri` | 销毁前导出的对象存储键；机器可重建，计算结果不行 |
| `error` | 最近一次失败原因，给运维看，不进模型上下文 |

每用户至多一台机器由部分唯一索引强制：

```sql
CREATE UNIQUE INDEX ux_sandbox_lease_one_per_user
    ON sandbox_lease (user_id)
    WHERE state IN ('provisioning', 'ready', 'paused');
```

`paused` 也在谓词里（与 `app/services/sandbox/lease.py:LIVE_STATES` 同一组取值）：只要它出现就算活着，同一用户再建一台会让用户看到两个 workspace。`destroying` 故意不在索引里：机器正在被拆，不能继续复用，但容量统计仍把它算作未结束，provider 确认之前 CPU/内存还没还给节点池。

状态迁移只能是一条条件 `UPDATE ... WHERE state = ...`，不能先读后写。回收循环、用户 reset、封禁清理可能同时在场，先读后写会把机器从长任务下面抽走。

`sandbox_command` 是审计表，不参与计费。`output_head` 存的是 **stdout 的截断视图**（头 4000 + 尾 2000 字符，见 `MAX_OUTPUT_HEAD_CHARS` / `MAX_OUTPUT_TAIL_CHARS`），不是原始输出：审计要回答「模型当时看到了什么」，而原始输出可能含用户数据，留在沙箱里。stderr 不落库，只影响 `truncated` 与 `output_bytes`。

## 删除语义

| 动作 | 数据库行为 |
|---|---|
| 删用户 | `accesstoken` / `sync_counter` / `conversation` / `message` / `sandbox_lease` 级联删除；沙箱命令随租约级联 |
| 删提供商 | `provider_model` 级联删除 |
| 删会话 | 会话行写 `deleted=true` 墓碑，不物理删；该会话的 `message` 行直接删掉。服务端没有过的会话 id 推墓碑不落库（只回 accepted），否则谁都能预占 id |
| 登出 / 踢人 | 删 `accesstoken` 行，会话立即失效 |
| 会话过期 | 启动时与每次登录成功后删掉 `created_at` 早于 `SESSION_LIFETIME_SECONDS` 的行（表上没有过期列，见上） |
| 封禁 | `user.is_active=false`，并拆掉该用户名下所有沙箱 |

## 相关源码

- `sql/init.sql` — 建表脚本，唯一事实来源
- `app/db/schema.py` — 启动执行与逐列漂移检测
- `app/db/maintenance.py` — 过期会话清理与 API Key 解密审计
- `app/db/models/user.py` — 用户、会话 token 摘要与 Redis 缓存
- `app/db/models/provider.py` — 提供商、模型与设置键值
- `app/db/models/sync.py` — 会话、消息与同步版本号
- `app/db/models/sandbox.py` — 租约状态机与命令审计
- `app/services/model_service/provider_repo.py` — 注册表初始化标记与提供商读写
- `app/services/sync_service.py` — 发号、pull/push 与游标
- `app/services/sandbox/lease.py` — 条件 UPDATE 与部分唯一索引的使用方
