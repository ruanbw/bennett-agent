# 设计：Agent 沙箱虚拟机（最终方案）

> 状态：**定稿**，取代 `docs/research/agent-sandbox-vm.md` 中 §8/§9 的阶段性结论。
> 调研过程与选型依据留在那份文档里；这里只写「做什么、用什么、怎么算做完」。
> 日期：2026-10-24。

---

## 1. 一句话

每个用户一台隔离环境（跨会话共用），由 **自托管 OpenSandbox** 编排（本地 Docker 运行时 /
生产 Kubernetes + Firecracker 运行时，同一套 API）；
第一次需要执行时自动创建，**心跳断了自动销毁**，销毁前把产物上传到 RustFS；
准入只需登录，计量只有 token。

---

## 2. 技术栈（逐层，含「不选什么」）

| 层 | 选型 | 为什么是它 | 不选 |
|---|---|---|---|
| 虚拟化基元 | **Firecracker**（KVM microVM，Rust）+ 自带 `jailer` | 设备面最小（无 PCI/virtio-mmio）、自带 seccomp/cgroup/chroot 沙箱、快照恢复被 Lambda/Fargate 验证 | QEMU（设备面大、无 jailer）、libkrun（本地嵌入用，非多租户服务端）、Cloud Hypervisor（留作 GPU/桌面场景） |
| 编排 | **OpenSandbox 自托管**（Apache-2.0，Linux Foundation，Alibaba 贡献） | 本地 Docker 运行时与生产 K8s+Firecracker 运行时**共用一套 API**；自带 egress 策略与 Credential Vault | 自写 Firecracker 编排（半个基础设施团队的量）、自己维护两套后端（docker + 云）、Daytona（2026-06 已归档闭源） |
| 起步形态 | 本地 `uvx opensandbox-server`（docker 运行时）；生产 K8s + 安全运行时（Firecracker/gVisor/Kata，**必须显式开**） | 本地零基础设施成本，线上同一套 API | 先上自写 K8s 调度（运维面远大于收益） |
| 模板 | 我们的 `docker/sandbox.Dockerfile` → 镜像 `bennett-sandbox:<version>`：python3.12 + pandas/matplotlib/pillow + git + ripgrep | 依赖装进镜像，运行时 0 安装 | 运行时 `pip install`（每次都要网、都不确定） |
| 控制面 | 现有 FastAPI + Postgres + Redis + RustFS(S3) | 复用既有栈，不引入新组件 | 新建服务（多一套部署与鉴权） |
| 心跳存储 | **Redis key + TTL**（`sbx:hb:{lease_id}`，EX=idle_ttl） | 每用户每 30s 一次写 Postgres 就是自伤的写放大 | 直接写 DB（P2 再考虑归档） |
| Agent 集成 | 现有 `ManagedTool` + `RiskLevel` + ContextVar 作用域（`knowledge_scope` 同款） | 不改 deps、不改 Agent 缓存键 | 新造 deps/RunContext 体系 |
| 前端 | React：心跳 hook + 「正在开机 / 已回收」状态（P1 再谈终端面板） | 现有 UI 已有 tool-call 状态可复用 | 首版做 xterm/noVNC（scope 外） |
| 本地开发 | `docker` provider，**仅**跑单测与 CI | 不需要 KVM 就能验证授权与生命周期 | 用 docker 后端冒充生产行为（网络/快照/冷启动都不一样） |
| 观测 | 现有 metrics + 新增 4 个告警项 | — | — |

**外部服务清单（部署时真正要买/要开的）**：本地零依赖（Docker + `uvx opensandbox-server`）；
生产是一个 K8s 集群 + 能拿到 `/dev/kvm` 的节点（裸金属，或开启嵌套虚拟化的实例）。

---

## 3. 拓扑

```
浏览器（React）
  │  SSE（每轮请求）+ 心跳（每 30s，页面可见且该会话激活时）
  ▼
FastAPI 控制面  ── Postgres: sandbox_lease / sandbox_command
  │              └ Redis: 心跳 TTL / 限流
  │              └ RustFS: 产物归档 artifact_uri
  │  HTTP + API token（编排 API）
  ▼
OpenSandbox server（本地：宿主机进程；生产：K8s 内的 server + operator）
  │  docker 运行时 或 kubernetes 运行时（Firecracker / gVisor / Kata）
  ▼
┌───────── 每会话一台 microVM（独立内核 / 独立 netns）─────────┐
│  envd（vsock + gRPC）· /home/sandbox/workspace · 出站过代理   │
└──────────────────────────────────────────────────────────────┘
```

硬规则：**控制面不在跑用户代码的机器上**。控制面握着 DB 凭据、LLM 网关 key、用户密文，
和工作负载同机等于把「逃逸」直接升级成「全站数据泄露」。

---

## 4. 生命周期

### 4.1 状态机

```
provisioning ──ready──> ready ──(idle/hard/主动)──> destroying ──> destroyed
      │                    ▲                            │
      └──失败──> failed     └── paused（provider 支持时）  └──失败──> destroy_failed（告警+重试）
```

**所有状态迁移都用条件 UPDATE**：`UPDATE sandbox_lease SET state='destroying' WHERE id=? AND state='ready' RETURNING id`，
rowcount=0 说明有人抢先了。没有这一步，reaper 会在一次长 `exec` 中间把机器拆掉。

### 4.2 两个时钟

| 时钟 | 默认 | 语义 | 到点 |
|---|---|---|---|
| `idle_ttl` | 10 min | `max(心跳, 活动)` 距今超过它 | 销毁 |
| `hard_ttl` | 4 h | 创建至今 | 销毁 |

- **心跳来源**：客户端（会话页面可见且该会话处于激活状态，每 30s `POST /api/sandbox/heartbeat`）
  → 写 Redis `EX idle_ttl`；
- **活动来源**：每次 `exec` / 文件读写由服务端更新 `last_activity`（DB）；
- reaper 判定：Redis 心跳键不存在 **且** `last_activity` 过期 → 销毁；
- **心跳绝不能绑 SSE**：SSE 是「一轮对话」的生命周期，轮次结束就断，绑它会导致每轮都超时回收、
  或者反过来被误判成活着。这是评审抓到的错误，写在这里防复发。

### 4.3 销毁

顺序不可颠倒：

1. `state → destroying`（条件 UPDATE 抢占）；
2. **导出产物**：`tar` workspace → 上传 RustFS → `artifact_uri`（大小上限 200 MB；
   超限只保留文件清单 + 单个最大文件的头部；模板自带的文件不计入）；
3. provider `destroy`（幂等）；失败则 `destroy_failed` + 重试 + 告警，**不允许静默**；
4. `state → destroyed`，写 `destroy_reason`（`idle` / `hard_ttl` / `user_deleted_session` / `user_banned` / `error`）。

**挂钩的强制销毁入口**：删除会话（sync 墓碑落地时）、用户被封禁、用户主动点「结束环境」。

### 4.4 创建

- 触发：本会话内第一次用到沙箱工具时**隐式创建**（用户不需要点按钮，注册即可用）；
- 幂等：`(user_id, session_id) WHERE state IN ('provisioning','ready')` 上建**部分唯一索引**，
  并发两次调用只会有一台机器，第二次读到同一行（agent 一轮里并发调两个工具是真实存在的）；
- 前端表现：复用现有 tool-call 的进行中状态显示「正在启动执行环境」，不新增事件类型。

---

## 5. 数据模型

```sql
create table sandbox_lease (
  id              text primary key,              -- sbx_<32 hex>，服务端生成
  user_id         uuid not null references "user"(id) on delete cascade,
  session_id      text not null,                 -- 最后使用它的会话（归因用，不参与寻址）
  provider        text not null,                 -- e2b | docker(dev)
  provider_ref    text,                          -- 编排侧 id / 容器名；只在服务端使用
  template        text not null,
  template_version text not null,
  state           text not null,                 -- provisioning|ready|paused|destroying|destroyed|failed
  created_at      timestamptz not null default now(),
  last_activity   timestamptz,
  hard_expires_at timestamptz not null,
  destroyed_at    timestamptz,
  destroy_reason  text,
  artifact_uri    text,
  error           text
);
create unique index ux_lease_one_per_session
  on sandbox_lease (user_id, session_id)
  where state in ('provisioning','ready','paused');
create index ix_lease_reap on sandbox_lease (state, hard_expires_at);

create table sandbox_command (            -- 审计 + 排障，不参与计费
  id            bigserial primary key,
  lease_id      text not null references sandbox_lease(id) on delete cascade,
  seq           int  not null,
  command       text not null,
  exit_code     int,
  duration_ms   int,
  output_bytes  int,
  output_head   text,                     -- 截断后的头部，供审计回看
  truncated     boolean not null default false,
  created_at    timestamptz not null default now()
);
create index ix_cmd_lease on sandbox_command (lease_id, seq);
```

**没有 `sandbox_usage`**：计量只有 token（§8）。

---

## 6. 工具契约（关键设计决定）

```python
sandbox_exec(command: str, timeout_s: int = 120) -> str
sandbox_write_file(path: str, content: str) -> str
sandbox_read_file(path: str, max_bytes: int = 200_000) -> str
sandbox_reset() -> str          # 扔掉当前会话的环境并重建（用户说「重来」时用）
```

**工具签名里没有任何 id。** 租约由服务端按 `(current_sandbox_scope() 的 user_id, session_id)` 解析。
这一条是评审的直接产物：原设计让模型传 `sandbox_id`，于是「换 id 越权」永远需要一串检查去兜；
把 id 从契约里删掉之后，**越权在构造上不可达**——模型手里没有可以伪造的句柄。

- 身份来源：`sandbox_scope(user_id, session_id)` ContextVar，在 `stream_chat_events` 里与
  `knowledge_scope(user_id)` 并列注入；取 `None` 时工具直接回「当前会话没有关联用户，无法使用沙箱」。
- `ChatRequest` 新增 `session_id`（可选字段；沙箱工具要求存在）。**它只用于归因**：机器按
  用户分配，会话之间共用一台（见 §15）。
  校验：若 `conversation` 表已有该 id，则必须 `user_id` 匹配；不存在则视为新会话（uuid4，客户端生成）。
- 风险等级：`sandbox_exec / write / read` = `READ_ONLY`（沙箱内可弃 = 可撤销，符合 `approval.py` 的
  「分界线是能不能撤销」）；`sandbox_reset` = `SAFE_WRITE`（销毁 + 重建，走一次审批）。
  审批闸门**一行代码都不用改**。

---

## 7. 授权：由构造保证，再叠三道纵深

| 道 | 机制 | 防什么 |
|---|---|---|
| 构造 | 工具不收 id；租约 = f(登录态 user_id, session_id) | 换 id 越权（不可能发生，而不是「被拦住」） |
| 归属 | `session_id` 属于该用户（会话行存在时必须匹配），所有查询带 `user_id` | 冒用他人的会话标识 |
| 纵深 | `provider_ref` 的 label/metadata 记 `owner=sha256(user_id)[:16]`，销毁/执行前比对一次 | DB 脏行、回滚、并发写错行（真正会把 A 的行喂给 B 的时刻） |
| 边界 | 错误文案统一「执行环境不存在或已回收」；不给存在性 oracle | 用响应差异探测他人环境 |

**不提供**：任何「用户直接 exec」的 HTTP 接口（多一个授权面）、任何按 `lease_id` 裸查的接口、
任何角色的跨用户 exec（含超管）。

---

## 8. 准入与计量

- **准入**：注册登录即可用，无套餐、无付费门、无专项额度；`capabilities.sandbox = true`。
- **计量**：只有 token（沿用 `pricing.py` + usage 链路）。沙箱的成本只有一条通路进产品——
  `exec` 输出进上下文变 token。因此 **`exec` 输出截断就是唯一的成本控制点**：
  保留头 4000 + 尾 2000 字符，附「共 N 行，完整输出在 `/tmp/…`」。
- **容量保护（不是付费分级）**：每用户 1 台（跨会话共用）；全局 = 节点池容量（16c/64G ≈ 10–12 台）；
  超限 `429` + 明确文案，**不静默排队**。数值全部进 settings。

---

## 9. 出站

生产默认 **白名单代理**（每条出站按域名授权 + 审批记录），不留自由联网。
理由：多租户下「模型把上下文里的内容写进沙箱再发出去」是真实路径，白名单是唯一能事后解释的形态。
`SANDBOX_EGRESS=open` 只保留给本地/自用 profile，且启动时打印告警。
沙箱内不允许 `-e` 透传宿主环境变量、不挂载任何宿主目录。

---

## 10. 交付阶段

### P0 — 契约与生命周期（无 provider 也能验收）
`app/services/sandbox/base.py`（6 方法契约）、`lease.py`（状态机 + 两个时钟 + 审计）、
`app/tools/sandbox_scope.py`、`app/tools/sandbox.py`、`docker/sandbox.Dockerfile`、
迁移（两张表）、`docker.py`（仅 CI）。
**DoD**：越权 5 用例全绿；状态机在并发 exec / 并发 create / reaper 竞态下不越界；`SANDBOX_ENABLED=false` 时零影响。

### P1 — 生产可用
`e2b.py` provider（自托管 Runtime）、心跳端点、前端心跳 hook + 「正在开机/已回收」状态、
产物导出到 RustFS、reaper 独立循环 + 告警、删除会话/封禁两个强制销毁挂钩、`capabilities` 上报。
**DoD**：真机上「首次使用自动开机 → 多轮续用 → 静置 10 分钟自动销毁 → 产物在 RustFS 可取」全链路通过。

### P2 — 企业加固
模板版本化与预热池、白名单出站代理、节点池容量与调度、观测面板（每会话资源采样 + 4 个告警）、
`sandbox_command` 审计回放 UI。

---

## 11. 评审发现（前几版方案里被改掉的东西，留档防复发）

1. **§9「本机 Docker 容器」与 §10「企业 microVM」冲突** → 容器只留作 CI/单测后端，生产是 microVM。
2. **「不改请求契约」与「每会话一台机器」冲突** → deps 不改，但 `session_id` 必须进 `ChatRequest`，
   否则服务端无法知道是哪台会话的机器。
3. **「自由联网」与「企业白名单」冲突** → 生产白名单，自由联网只留本地 profile（§9）。
4. **心跳不能绑 SSE**（SSE 是一轮对话的生命周期）→ 绑「页面可见 + 会话激活」（§4.2）。
5. **工具收 `sandbox_id` 会留下存在性 oracle 与越权面** → 从契约里删掉 id，由作用域解析（§6）。
6. **reaper 会拆掉正在跑长任务的机器** → 条件 UPDATE + `destroying` 中间态（§4.1）。
7. **并发建机会建出两台** → 部分唯一索引 + 创建幂等（§4.4）。
8. **销毁前「保产物」没有边界** → 200 MB 上限 + 超限保留清单（§4.3）。
9. **删除会话 / 封禁用户没有挂钩** → 两个强制销毁入口（§4.3）。
10. **每 30s 心跳写 Postgres 是自伤** → Redis TTL（§2）。

---

## 12. 实现状态

### 已完成（P0，2026-10-24）

| 文件 | 内容 |
|---|---|
| `app/services/sandbox/base.py` | provider 契约（6 方法 + `owner_tag` 二次校验 + 输出截断常量） |
| `app/services/sandbox/opensandbox.py` | OpenSandbox 后端：SDK 封装、metadata 归属核对、超时翻译（`exit -1` → `timed_out`）、`is_alive` 用连接探测 |
| `app/services/sandbox/lease.py` | 租约状态机、两个时钟、容量保护、条件 UPDATE 抢占、审计发号（`FOR UPDATE` 父行） |
| `app/tools/sandbox_scope.py` | `(user_id, session_id)` 作用域 ContextVar，与 `knowledge_scope` 同机制 |
| `app/tools/sandbox.py` | 四个工具（**无 id 参数**）+ `render_exec_result` 输出截断 |
| `app/db.py` / `migrations/versions/d6dcd0bc90b8_*.py` | 两张表 + 部分唯一索引；`test_db_schema` 的漂移与上下行往返通过 |
| `docker/sandbox.Dockerfile` | python3.12 + pandas/matplotlib/git/rg，uid 1000 非 root |
| `scripts/smoke_sandbox.py` | 真机冒烟（对着真 server，9 项全过） |
| `docker/opensandbox.toml` | 本地 server 配置（docker 运行时 + egress 策略 + 能力裁剪） |
| 测试 | 56 个沙箱用例：越权 5 条、并发抢占、销毁失败可重入、输出截断、迁移往返 |

全量套件通过（沙箱相关 35 个用例：越权 5 条、并发抢占、销毁失败可重入、输出截断、迁移往返）。
先前的自写 docker 后端（`docker.py` + 其 23 个单测）已按要求删除：OpenSandbox 的 docker 运行时
覆盖了同一场景，且与生产共用一套 API。

### 与设计稿的两处偏差（实现时发现，已按更严的一侧收敛）

1. **`mark_destroying` 允许从 `destroy_failed` 重入**。原设计只写了 `ready/paused`，
   那会把「销毁失败」变成终态——机器还在跑却没人再关它。失败必须是可重试状态。
2. **`ensure_lease` 在 `create` 之前提交 `provisioning` 行**。否则并发第二路会被唯一索引
   阻塞整个冷启动过程；提交后它读到「正在启动」并快速返回，比排队 30 秒正确。

### 已完成（P1，2026-10-24）

| 文件 | 内容 |
|---|---|
| `app/services/sandbox/reaper.py` | 回收循环（心跳复核 → 原子抢占 → 导出 → 销毁 → 落终态）+ 两个强制销毁入口 |
| `app/services/sandbox/artifacts.py` | 产物导出：体积超限退化成「只给清单」，上传失败只告警不挡销毁 |
| `app/api/sandbox.py` | `POST /api/sandbox/heartbeat`（只写 Redis、永不泄漏有无机器）、`GET /api/sandbox/status` |
| `main.py --worker` | 回收循环与上传解析 worker 同一个 stop 事件、`asyncio.gather` 并列 |
| 挂钩 | 删除会话（sync 墓碑提交后）、封禁用户（admin）都会立刻拆机器 |
| 前端 | 聊天请求带 `session_id`；`useSandboxHeartbeat`（页面可见才发、切回立刻补一次）；capabilities 增加 `sandbox` |

两处顺带修掉的**设计漏洞**（评审时只改了一半）：

1. `find_destroyable` 原本不含 `destroy_failed`，所以「销毁失败」既不会被重试、也不被
   当成活动租约——机器永久泄漏。现在候选集包含它，且 reason 沿用当初记录的
   `destroy_reason`（不按当前时间重算，否则归因会写错）。
2. `mark_destroy_failed` 原本只接受 `destroying`，卡死的 `provisioning` 残留无法标记；
   现在两者都接受（后者只标记 + 告警，不猜着销毁——按空 ref 去删只会失败）。

### 待做（P2）

预热池（SDK 自带 pool 子系统，把 4.3s 冷启动压到百毫秒级）、K8s Fast Sandbox 压测、
出站白名单的具体域名策略、审计回放 UI、`sandbox_status` 的状态卡片。

---

## 13. 追加评估：OpenSandbox（2026-10-24 晚）

### 事实核对

- 仓库 `opensandbox-group/OpenSandbox`（原 `alibaba/OpenSandbox`，已迁组织），**Apache-2.0**，
  Linux Foundation 项目、Alibaba 贡献；创建于 2025-12，**16.1k stars / 1.5k forks**。
- 两个运行时：**Docker**（本地）与自研的 **K8s "Fast Sandbox"**（Firecracker；
  自报 64ms P50 串行 / 125.4ms P99 十并发，支持 pause/resume 保留内存与磁盘）。
- 自带能力里有两项**正是本设计的 P2**：per-sandbox **egress policy**（mitmproxy + nftables + DNS
  nameserver）与 **Credential Vault**（agent 拿不到真凭据，只拿代理令牌）——后者与 Muse 的
  `hatch-authd` 是同一个思路。
- SDK：Python / Java / TS / C# / Go，另有 `osb` CLI 与 MCP；API 形状
  `Sandbox.create() / commands.run() / files.write_files() / files.read_file() / destroy()`。
- OSEP-0004 表明 gVisor / Firecracker / Kata 是**server 级别显式配置**的安全运行时，
  多租户必须显式打开，默认容器运行时不是隔离边界。

### 关于「star 最高」

不是最高，但很可能是「**活跃且可自托管**」这一类里最高的：Daytona 有 71.5k stars，但
2026-06 已归档、核心转闭源，不能作为长期依赖；E2B 的 star 数低于它，胜在生产验证（Manus）。

### 结论：生产后端从 E2B 改为 OpenSandbox

理由不是热度，是它把三件事一起解决了：

1. **dev 与 prod 同一个 API**：本地 Docker 运行时、生产 K8s+Firecracker，我们的
   `SandboxProvider` 契约 1:1 映射（`create/exec/put_file/get_file/destroy`）；
2. **egress 白名单**（原 P2）与 **Credential Vault**（原设计里「凭据不进沙箱」的落点）现成；
3. 可自托管 + Linux Foundation 治理，企业招标时「数据不出机房」这一条成立。

保留的顾虑（写进风险，不当作已解决）：

- 项目年轻（约 10 个月），`oseps/` 说明协议仍在演进，升级要跟版本；
- K8s 编排是自研实现，运维复杂度高于「单机 compose」；生产规模前需要压测；
- **必须显式开启 Firecracker/安全运行时**，否则默认容器运行时在多租户下不合格。

### 对已交付代码的影响

`app/services/sandbox/base.py` 的契约、租约层、工具层、迁移、测试**全部不变**。
只需新增 `app/services/sandbox/opensandbox.py`（实现同一协议），并：
- `settings.sandbox_provider` 增加 `opensandbox` 取值；
- 规划中的 `e2b.py` 取消；
- `docker.py` 保留为「不依赖额外服务的 CI 快径」，但端到端冒烟应跑 opensandbox 后端。

---

## 14. 部署指南

### 14.1 本地（现在就能用）

```bash
# 0. 数据库结构（首次或拉取新代码后）：应用启动会校验 revision，落后就拒绝启动
uv run alembic upgrade head

# 1. sandbox server（配置在仓库里：docker/opensandbox.toml）
OPENSANDBOX_INSECURE_SERVER=YES uvx opensandbox-server --config docker/opensandbox.toml
#    → 监听 127.0.0.1:8080，`curl /health` 返回 200 即就绪

# 2. 沙箱镜像（server 建机时用它）
docker build -f docker/sandbox.Dockerfile -t bennett-sandbox:0.1 .

# 3. 契约冒烟（不碰数据库/登录态，只验证 provider）
uv run python scripts/smoke_sandbox.py

# 4. 打开
#    .env: SANDBOX_ENABLED=true / SANDBOX_SERVER_URL=http://127.0.0.1:8080

# 5. 起后端：**一条命令**，API + 上传解析 + 沙箱回收同进程
uv run python main.py
```

注意事项：

- server 用 **sqlite** 存沙箱记录（`~/.opensandbox/opensandbox.db`），一台机器一个进程；
- 不设 API key 时必须显式 `OPENSANDBOX_INSECURE_SERVER=YES`（否则交互式要求输入 YES）；
- macOS 上若出现 endpoint 拉取失败，把 `[proxy] resolve_internal` 改成 `false`（配置里已注明）；
- 实测冷启动约 **4.3s**：这是 SDK 的「等 endpoint + 健康检查」预算，不是容器启动慢。
  前端按「正在开机」呈现；要压到百毫秒级靠 K8s Fast Sandbox 或预热池（§12 P1）。

### 14.2 生产（K8s + 安全运行时）

1. **节点**：沙箱宿主要能拿到 `/dev/kvm`（裸金属，或宿主已开启嵌套虚拟化的云主机）。
   拿不到 KVM 就只能退到 gVisor/Kata，隔离等级与性能都不同，需要重新压测。
2. **部署 server**：用官方 Helm chart（仓库 `deploy/` + `helm-release-test.yml` 在维护）或直接跑
   `opensandbox-server` 容器；按官方 operator 文档装 control-plane operator（`workload_provider`
   默认 `batchsandbox`，也可用 `agent-sandbox`）。
3. **配置**（对照 `docker/opensandbox.toml` 改）：
   ```toml
   [server]
   host = "0.0.0.0"
   api_key = "<强随机串>"          # 生产必配，且只在内网暴露
   max_sandbox_timeout_seconds = 14400

   [runtime]
   type = "kubernetes"
   execd_image = "opensandbox/execd:v1.1.0"

   [store]
   type = "postgresql"            # 多副本 server 必须共享一个库
   ```
   DSN 走环境变量 `OPENSANDBOX_STORE_POSTGRESQL_DSN`，不要写进文件。
4. **显式开启安全运行时**（Firecracker / gVisor / Kata）。这一步漏了，多租户隔离就退回
   「共享宿主内核 + 能力裁剪」——这是本方案唯一不能省的一步。
5. **应用侧只改三个值**：`SANDBOX_SERVER_URL`（集群内 ingress）、`SANDBOX_SERVER_API_KEY`、
   `SANDBOX_TEMPLATE_VERSION`（镜像版本与租约记录必须对得上）。
6. **egress**：保持 `SANDBOX_EGRESS=proxy`，域名白名单在 server 侧配置；`open` 只给本地。

### 14.3 升级与回滚

- server 与 execd 镜像版本要一起升（`execd_image` 与 server 版本绑定）；先升 server 再升镜像；
- 模板版本化：`SANDBOX_TEMPLATE_VERSION` 前滚后，旧租约仍记着旧版本，排查时能对上；
- 回滚只需把 `SANDBOX_TEMPLATE_VERSION` 指回旧 tag——已存在的沙箱不受影响，新机用旧镜像。

---

## 15. 变更：一会话一台 → 每用户一台（2026-10-24）

需求：同一用户在会话 1 里开过机器，会话 2、3 用到时**复用同一台**，不要一个会话建一个。

改动（三层都必须改，漏一层就是「界面一套、库里另一套」）：

1. **唯一索引**：`(user_id, session_id)` → `user_id`（部分索引，仅对活动状态生效）。
   迁移 `d7df7000bda7` 在创建新索引前，把同一用户「除最新一台以外」的活动租约标成
   `destroy_failed` —— 回收循环的候选集包含这个状态，于是多余机器会被正常拆掉，
   而不是在迁移里直接删行（删行等于留下没有主人的机器）。
2. **寻址**：`get_active_lease(user_id)` / `get_latest_lease(user_id)` 不再接收
   `session_id`；`list_active_for_session` 删除。复用时会改写 `session_id`
   （「最后是谁在用」）。
3. **删会话不再拆机器**：机器可能正被该用户的其他会话使用。改为完全交给空闲/硬上限回收；
   封禁用户仍然立刻拆（那是「收回算力」，不是「清理一个会话」）。

**代价（明确接受）**：同一用户的不同会话共享 `/home/sandbox/workspace`，一个会话写的文件
另一个会话也看得见。它们属于同一个人，不是跨租户泄漏；工具描述与系统提示词里都写明了
「别的会话装过的依赖也在这里，先看一眼再决定要不要重装」。

**测试锁住的行为**：两个会话 → 一台机器、`session_id` 跟随最新会话；删会话后租约仍是
`ready`；封禁用户 → 机器被拆且记 `user_banned`。
