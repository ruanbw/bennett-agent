---
title: 沙箱执行
description: 每用户一台执行环境：租约状态机、心跳与回收、容量、命令审计、reset 审批，以及 provider 契约。
---

沙箱让模型能在**用户自己的**隔离环境里跑命令、读写文件。机器按用户分配：**一个用户跨所有会话共用一台**，不是一会话一台。

## 按用户一台，不是按会话

寻址只看 `user_id`。`session_id` 只记「最后是谁在用」，纯归因用途（排障时看这台机器最后给哪个会话用了），不参与寻址。

原因有两条：

- 用户在另一个标签页问「刚才那个文件还在吗」，如果按会话分配，答案只能是「不在」——同一个人的两台机器彼此看不见。
- 沙箱是重资源（默认 2 vCPU / 4 GB）。一个用户开十个会话就占十台。

数据库层面靠一个部分唯一索引兜底（`sql/init.sql`）：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_sandbox_lease_one_per_user
    ON sandbox_lease (user_id)
    WHERE state IN ('provisioning', 'ready', 'paused');
```

`paused` 也算活着——它随时能恢复，同一会话再建一台会让用户看到两个 workspace。

### 工具签名里没有身份

四个沙箱工具的签名里没有 `sandbox_id` / `user_id`。租约由服务端按 `current_sandbox_scope()` 解析，作用域由服务端从登录态注入。模型手里没有可伪造的句柄，越权在构造上不可达。

`session_id` 也不是身份：客户端可以谎报，但租约查询一律带 `user_id`，谎报只会拿到本用户自己的机器。

## 租约状态机

状态取值与 DB 里的字符串一致，历史行不会跟着迁移：

```text
                    create 成功
   provisioning ──────────────────▶ ready
        │                            │  │
        │ 建机失败                    │  │ mark_destroying
        ▼                            │  ▼
      failed                    paused ┌──────────────┐
                                     │  destroying   │
                                     └──────┬────────┘
                                            │ destroy 成功
                                            ▼
                                       destroyed
                                            ▲
                              destroy 失败 ───┘  (destroy_failed 会被重抢)
```

实际的状态集合：`provisioning` / `ready` / `paused` / `destroying` / `destroyed` / `failed` / `destroy_failed`。

| 状态 | 含义 | 是否占容量 |
|---|---|---|
| `provisioning` | 控制面在建机，`provider_ref` 还没拿到 | 是 |
| `ready` | 可执行 | 是 |
| `paused` | 暂时挂起，随时能恢复 | 是 |
| `destroying` | 正在拆 | **是** |
| `destroyed` | 已销毁，终态 | 否 |
| `failed` | 建机失败（终态） | 否 |
| `destroy_failed` | 销毁失败，等重试 | 否 |

`destroying` 也算占容量：provider 确认之前那台机器的 CPU/内存还没还给节点池，按它不占容量放行会打爆节点池。

### 状态迁移只能是一条条件 UPDATE

检查与改必须在同一条 `UPDATE ... WHERE state = ...` 里。这是并发正确性的基础：

- `mark_destroying` 抢销毁权，抢不到返回 `False`。回收循环与用户主动 reset 会同时在场，先读后写会把机器从长任务下面抽走。
- `mark_destroyed` 只允许从 `destroying` 迁移。
- `mark_destroy_failed` 接受 `destroying` 与 `provisioning`。

`destroying` 停留超过 `DESTROYING_STALE_SECONDS`（15 分钟）才算卡住，允许重抢。无条件地允许重抢会让两个并发回收者都满足条件各自提交。

### 建机的顺序不能反

```text
查活动租约 → 容量检查 → 插 provisioning 行 → commit
                                      ↓
                             provider.create()（建机）
                                      ↓
                     条件 UPDATE: provisioning → ready + provider_ref
```

先插行再建机。反过来的话并发会建出两台机器，而唯一索引只保护得了数据库。

建机失败（连 `CancelledError` 一起拦）时把行改成 `failed` 并独立提交：被取消的那台机器可能已经建出来了，停在 `provisioning` 的行会让这个会话以后每次都读回一台没有 ref 的租约。

### 一个不能自愈的边界

建机期间这行被别人从 `provisioning` 挪走时，代码会**响亮地失败**而不是猜着自愈：

```text
租约在建机期间离开了 provisioning（lease_id=..., ref=...）：机器需要人工回收。
```

因为这台机器的 ref 只在这一处出现过，静默吞掉就永远查不到它。

## 心跳与回收

回收必须**常驻**：触发条件是「没人发请求了」，挂在请求路径上就永远不触发。`main.py` 在同一个事件循环里起 `run_reaper(stop)` 任务。

### 两个时钟

| 时钟 | 存在哪 | 记什么 |
|---|---|---|
| `last_activity` | PostgreSQL | 服务端观察到的工作（exec / 文件读写的开始与结束） |
| 心跳 | Redis TTL 键 `sbx:hb:<lease_id>` | 客户端还在看这个会话 |

心跳走 Redis 是因为它是纯写放大：每 30 秒一次数据库 UPDATE，而键没了就等于心跳断了。TTL 取 `sandbox_idle_seconds`——一次心跳等于再给一个空闲窗口。

`last_activity` 与心跳的分工：心跳证明**客户端还在**，活动证明**机器还在被用**（用户关了页面但 agent 还在跑长任务）。

### 回收判定

回收循环每 60 秒跑一轮（`REAP_INTERVAL_SECONDS`），判定顺序：

```text
find_destroyable  →  候选集（不是「该杀的」）
     ↓
heartbeat_alive  →  心跳还活着就跳过
     ↓
mark_destroying  →  条件 UPDATE 抢销毁权
     ↓
export_workspace →  导出产物到对象存储
     ↓
destroy          →  拆机器
     ↓
mark_destroyed   →  落终态
```

候选集只给「可能该杀的」，调用方必须逐个复核心跳后再动手。心跳在 Redis、候选在数据库，两处时钟不共用一个 `now`，所以回收原因随候选一起返回（`DestroyCandidate.reason`），不在判定时重算——重算会让归因写错。

回收原因：`idle` / `hard_ttl` / `session_deleted` / `user_banned` / `user_reset` / `error`。

销毁失败**不是终态**：落成 `destroy_failed`，下一轮回收会重新抢它（`mark_destroying` 允许重抢、`find_destroyable` 放进候选）。少一条路径就永久泄漏机器。

Redis 故障时回收会保守跳过这一轮：返回 `False` 会让回收循环瞬间拆掉全部机器。

### 两个绝对上限

| 上限 | 默认 | 语义 |
|---|---|---|
| `sandbox_idle_seconds` | 600 秒 | 空闲回收：超过 `max(心跳, 最近活动)` 就销毁 |
| `sandbox_hard_ttl_seconds` | 14400 秒（4 小时） | 绝对上限：到点无论有没有心跳都销毁，产物照样先导出 |

只靠空闲回收的话，一个挂着不动的标签页可以永远占机。

## 容量

两个上限，都是**容量保护**而不是付费分级：

| 配置 | 默认 | 检查方式 |
|---|---|---|
| `sandbox_max_per_user` | 1 | 数该用户 `OPEN_STATES` 的租约数 |
| `sandbox_max_concurrent` | 12 | 数全站 `OPEN_STATES` 的租约数 |

超限直接抛 `SandboxCapacityError`，**不静默排队**。全局锁会「节点池慢」升级成所有人排队；并发可能少量超发，这是有意的取舍。

给模型的文案是「当前执行环境资源已满，请稍后再试。」——池满与「另一路正在建机」共用这一句，因为对模型来说下一步都是「稍后再试」。

## 命令超时与输出截断

### 超时

模型给的 `timeout_s` 会被夹到 `[1, 900]`，默认 120 秒。夹紧而不是拒绝：越界值多是模型随手写的大数。

provider 必须自己保证到点终止——取消协程后容器里的进程还在跑，下一次 exec 会和它抢同一个 workspace。所以 OpenSandbox 后端把超时翻译成 `exit_code == -1` + `timed_out=True`，而不是抛异常。

超时文案是「命令超时（可调大 timeout_s 或缩小范围）」，不是「环境坏了」——后者会让模型去重建沙箱重跑同一条命令，只多花一次冷启动。

### 截断：前 4000 / 后 2000

```text
stdout：前 4000 字符 + ……（中间省略 N 个字符）…… + 后 2000 字符
stderr：前 1500 字符 + ……（中间省略 N 个字符）…… + 后 1500 字符
```

**必须留尾**：关键信息常在最后（安装结论、报错末行、`tail` 的输出），只截头会让模型看 4000 个进度条。

stderr 阈值比 stdout 短：几万行 stderr 多是重复报错，对新信息没有贡献。

截断在渲染层做，provider 返回的是未截断原文。阈值来源在 `app/services/sandbox/base.py`，是成本控制——一次 `npm install` 就能把一轮对话的 token 成本推高一个数量级。

## 命令审计

每次 exec 都写一条审计（`sandbox_command` 表）：

| 字段 | 内容 |
|---|---|
| `seq` | 租约内的序号，靠先锁租约行再取 `max(seq)+1` 保证并发唯一 |
| `command` | 清理后的命令原文 |
| `exit_code` / `duration_ms` / `output_bytes` | 执行元信息 |
| `output_head` | **渲染后的**截断文本（模型当时看到的就是这个） |
| `truncated` | 是否发生了截断 |

审计不参与任何金额计算——计量只有 token。审计写失败只记日志：命令已经跑完，统计写入失败不该变成「环境不可用」。

## reset 审批

`sandbox_reset` 的风险等级是 `safe_write`，执行前需要用户批准——它会销毁现有机器，下次执行时重新创建一个干净的，已安装的依赖与工作目录里的文件全丢。

流程（`app/tools/sandbox.py:sandbox_reset`）：

```text
claim_for_reset(user_id)
   ├─ lease is None  → 「还没有创建执行环境，无需重置」
   ├─ 没抢到销毁权    → 「正在创建或回收中，本次没有执行重置」
   └─ 抢到了
        ├─ provider.destroy() 成功 → mark_destroyed → 「已重置，下次执行会创建新环境」
        └─ 销毁失败              → mark_failed（destroy_failed）→ 失败文案
```

销毁要求**幂等**：机器已经没了就是成功，不该让用户看到一次失败。停在 `destroying` 的租约既不被回收循环重试、也不是活动租约，是最容易永久泄漏机器的中间态——所以失败必须落成 `destroy_failed`，告警与重试才有东西可查。

reset 不当场重建新机器：连续两次重置才不会白开两台。

## provider 契约

契约在 `app/services/sandbox/base.py`，是冻结接口——换后端不该动业务代码。

```python
class SandboxProvider(Protocol):
    name: str

    async def create(spec: SandboxSpec) -> SandboxHandle: ...
    async def exec(ref, command, *, timeout_s, workdir, owner_tag) -> ExecResult: ...
    async def put_file(ref, path, data, *, owner_tag) -> None: ...
    async def get_file(ref, path, *, max_bytes, owner_tag) -> bytes: ...
    async def destroy(ref, *, owner_tag) -> None: ...
    async def is_alive(ref) -> bool: ...
```

几条约定：

- **失败必须用异常表达**，否则工具层只能猜。异常类型：`SandboxError` / `SandboxNotFound` / `SandboxCapacityError` / `SandboxTimeout`。
- **实现必须幂等、并发安全**，并自己保证资源不泄漏。
- **`owner_tag` 必须写进 provider 元数据并比对**：`sha256(user_id)[:16]`。用哈希而非原始 uuid——容器 label 会出现在日志与 `docker inspect` 里。归属不符抛 `SandboxNotFound`，且**不区分「不存在」与「不属于你」**：对外文案必须一致，否则这工具就是存在性探测器。
- **provider_ref 只在服务端流动**，从不出现在工具签名里。
- `get_file` 超 `max_bytes` 要抛错，不要静默截断。

### 后端

目前只有一个后端：`opensandbox`。本地用 Docker 运行时，生产用 K8s + Firecracker，两者共用同一套 API 与实现——不需要两个后端。

OpenSandbox 实现里有两个实测坑：

- 命令不能传 `uid/gid`，否则 `exit_code` 变 `None`、`stdout` 为空。
- 超时的表现是 `exit_code == -1` + `CommandExecError` 而非抛异常，必须翻成 `timed_out=True`。

## 产物导出

机器可以重建，用户算出来的结果不行。销毁前把 workspace 打包上传到对象存储（S3 协议）。

- 体积 ≤ `sandbox_artifact_max_bytes`（默认 200 MB）→ 打包成 `workspace.tar.gz`。
- 超限或量不出来 → 只导文件清单 `manifest.txt`（最多 200 条，按大小降序）。把「读不到」当成「很小」正是会把控制面读爆的那一次。
- **导出失败也要继续销毁**：否则对象存储抖一下就永久占着内存。
- 对象键带 `lease_id`：`sandbox/{user_id}/{lease_id}/{filename}`。同一会话重建多次环境，不带 lease_id 会让后一次导出覆盖前一次。

对象存储未配置时导出静默跳过，只告警。

## 不可用时的降级

| 情形 | 表现 |
|---|---|
| `SANDBOX_ENABLED=false` | 四个工具不装配，模型看不到；提示词改写成「本轮没有隔离执行环境……不要假装跑过，也不要编造运行结果」 |
| 沙箱服务不可达 | 工具返回「执行环境暂时不可用，请稍后再试。」，对话其余部分照常 |
| 租约不存在或已回收 | 「执行环境不存在或已回收，请重试；下次执行会自动创建新环境。」 |
| 池满 | 「当前执行环境资源已满，请稍后再试。」 |
| 命令超时 | 「命令超时（可调大 timeout_s 或缩小范围）。」 |
| 没有登录 / 没有会话标识 | 「当前会话没有关联用户（未登录），无法使用执行环境。请直接依据对话内容回答，并说明你没有可用的执行环境。」 |

沙箱工具的失败**永远是返回文案，不抛异常**——工具契约是「永远返回文案，不把异常抛给模型」。

## 相关源码

- `app/services/sandbox/base.py` — provider 契约、截断阈值、超时上限、`owner_tag`
- `app/services/sandbox/lease.py` — 租约状态机、两个时钟、命令审计
- `app/services/sandbox/lease_service.py` — 一次工具调用需要哪几次 lease 写操作
- `app/services/sandbox/reaper.py` — 回收循环与强制销毁入口
- `app/services/sandbox/opensandbox.py` — OpenSandbox 后端实现
- `app/services/sandbox/artifacts.py` — 销毁前的产物导出
- `app/tools/sandbox.py` — 四个工具的契约与渲染
- `app/runtime/scopes.py` — 沙箱作用域的注入与读取
- `sql/init.sql` — `sandbox_lease` / `sandbox_command` 表与唯一索引
