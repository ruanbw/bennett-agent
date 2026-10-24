# 调研：给 Agent 一个「虚拟机」——产品现状、技术选型与对本项目的影响

> 调研时间：2026-10-24。所有价格与版本都会变，落地前需复核。
> 标记约定：🟢 = 官方文档/官方博客一手信息；🟡 = 第三方工程分析（可信但非官方口径）；🔴 = 无一手来源，推断。
>
> ⚠️ **本文档只作调研留档，不是方案。定稿方案见 [`docs/design/sandbox-vm.md`](../design/sandbox-vm.md)。**
> 下文 §8 / §9 是阶段性结论，已被 §10 与设计文档取代，阅读时不要拿它当最终形态。

---

## 0. 先把词捋清楚：产品说的「虚拟机」通常不是虚拟机

「让用户做某些操作时启动一台虚拟机」这个说法来自产品文案，不是技术定义。实际实现分布在一个谱系上，
**隔离边界在哪一层，决定了它能做什么、要花多少钱、以及我们实现起来有多难**：

| 隔离等级 | 实现 | 逃逸代价 | 能不能跑浏览器/桌面 | 冷启动 | 典型产品 |
|---|---|---|---|---|---|
| 应用级 | 解释器沙箱、AST/白名单 | 极低 | ❌ | ~0 | 部分「代码解释器」功能 |
| 容器（共享内核） | Docker / OCI + seccomp | 内核 0day 即逃逸 | 勉强 | 100ms–2s | Codex cloud、Claude Code cloud 🟢 |
| 微虚拟机 | Firecracker / Cloud Hypervisor / libkrun | 需打穿 hypervisor | ✅ | 150ms–1s | E2B、Fly、microsandbox 🟢 |
| 完整 VM | QEMU/CH + 全量依赖 | 同上但更重 | ✅ 带 GUI | 数秒–数十秒 | Muse、Freestyle、Devin 快照 🟢 |
| 物理机/专用机 | 独占主机 | — | ✅ | 分钟 | 自托管 Outposts 类方案 |

真正的分水岭只有两个问题：

1. **隔离边界画在哪一层？**（模型写的东西有没有可能碰到我们的 Postgres / .env / 用户上传的原件）
2. **谁持有凭据？**（沙箱里的 agent 能不能看到 API key、OAuth token、用户密码）

只回答第 1 个问题的产品，本质是「远程代码执行服务」；回答了第 2 个的，才是「个人 Agent 的操作系统」。

---

## 1. Muse（Meta，2026-09 发布）——目前最激进的参照物

它被拿出来当例子是对的：Muse 不是「给 agent 加了个代码执行工具」，而是**先把用户的整台云电脑造出来，
agent 只是这台机器上的一个受限进程**。官方原文的定位：每个用户一台专属 VM，Ubuntu + Chromium + 文件系统 +
终端，7×24 常驻，凭据存在这台 VM 里而不是 Meta 的中心化设施里 🟢。

### 1.1 两层隔离，不是一个沙箱

官方的用词是「同一台机器上两个隔离的安全域，而不是一个带 root 的 LLM agent」🟢。
逆向分析进一步确认了结构 🟡：

```
Cloud Hypervisor microVM（KVM，DMI 报 cloud-hypervisor，用 stock Ubuntu 内核）
└── VM 内 systemd：spawnd / sentinel / authd / hatch-safety / postgres / browser-broker / 反向代理
    └── systemd-nspawn 运行时 cell（htch-runtime）
        ├── hatch daemon（agent harness，327MB Rust 单二进制，从 cell 外 nsenter 进来）
        ├── hatch-execd（起用户的 shell/进程）
        ├── /home/hatch → vdd：LUKS2 加密的 100GB「Reliable Volume」，持久
        └── /opt/hatch  → vdc：squashfs 只读 + verity（工具、skills）
```

cell 的硬化手段（官方 🟢，逆向细节 🟡）：

- cell 内的 root 映射到 VM 上的非特权用户，**不是 host root**；
- 独立 rootfs，与放敏感数据的宿主文件系统分开；
- 系统调用过滤（例如禁掉 `io_uring`）、能力裁剪（无 `CAP_SYS_PTRACE`、无 `CAP_NET_ADMIN`）；
- LSM 全开：`landlock,lockdown,yama,integrity,apparmor,bpf`；
- **推理 socket 根本不挂进 cell**——agent 看不到模型 endpoint，也没法拿自己的 key；
- cell 与宿主服务之间走 Unix domain socket + `SO_PEERCRED` + peer ACL（内核认证，无秘密可偷）；
- agent `pip install` 的包只记成「意图账本」，重放而不是永久写进 rootfs。

### 1.2 三个宿主侧服务，把「权限」从模型手里拿走

| 服务 | 职责 | 为什么必须在 cell 外 |
|---|---|---|
| `hatch-safety` | 独立模型/分类器审输入输出、识别 prompt injection | 让被攻击的 agent 无法关掉自己的防护 |
| `privsep` workers | 跑内置连接器业务逻辑，按 cgroup 做凭据白名单 | 日历 worker 改个参数也拿不到邮箱凭据 |
| `hatch-authd` | 凭据存储 + 代币「代理人化」 | 主 agent 永远看不到真 token |
| `Sentinel` | **唯一**有权批准「连接器动作」和「全部网络出站」的实体 | agent 只能提议，不能授权 |

Sentinel 的判定是三态：`allow / deny / ask`。出站管控做得比大多数产品都细 🟢：

- 前向代理 + userns/veth 边界 + eBPF 过滤；L4 和 L7 都看（hostname、DNS 解析后的真实 IP、端口、方法、路径、解码后的 body）；
- SSRF 防护针对「看起来是公网域名、解析后落到内网」的 DNS rebinding；
- **JIT 凭据注入**：cell 里的代码只拿到 `authd` 造的 surrogate token，真凭据在代理出站那一刻才替换进去。所以「骗 agent 泄露密码」在结构上无收益；
- **tainted egress（污点出站）**：用 eBPF cgroup 程序 + 自定义 LSM hook 做进程级数据流追踪。每个工具进程从「干净」开始，读过用户数据就被打污点；只有**干净且命中窄白名单**的请求才自动放行，有污点的回落到人工审批。这是为了解决「全都问用户」的信噪比问题。

逆向补充的关键实现细节 🟡：DNS 由网关回**合成地址**（fake-IP），这样每条连接在代理侧都能归因到一个域名；
`spawnd` 再挂一层 eBPF `connect4/connect6` cgroup hook，用 `BPF_F_ALLOW_MULTI` 组合——
**每个程序都放行才算放行**，所以 Sentinel 挂了也不等于内网可达。代理密码和 CA 每次换机轮换。

### 1.3 审批是 capability，不是聊天里的一句话

- 审批弹窗由 Sentinel **直接推给客户端**，不经过对话流——被注入的模型没法伪造或诱导用户点确认；
- 授权有作用域：一次性 / 会话级 / 任务级 / 限时 / 永久，且**绑定到具体连接器、目标、用途**，后续调用必须精确匹配；
- 读操作、此前已授权、可证明低风险的动作不打断用户。

### 1.4 浏览器是另一个 VM

🟡 Muse 的浏览器跑在**按任务租用的独立 browser VM** 里；CDP 连接由 cell 外的 broker 管；
浏览器子 agent 只看到**可访问性树快照**，拿不到原始 DOM，不能执行页面 JS，devtools 关闭；
用户接管或凭据自动填充时 agent 被暂停 🟢。密码走「客户端 → authd → cell 外的凭据库 → 在该填的那一刻注入浏览器」，
主 agent 全程不可见 🟢。

### 1.5 代价：冷启动是真的慢

🟡 预热的 hatchling + 嫁接 Reliable Volume：**13s 到 cell，约 40s 到就绪**；空闲时用 balloon 回收内存，
按 rollout 整机替换。对比 Claude Code ≈6.4s 到 harness、E2B 微虚拟机在几百 ms 量级。
**这意味着体验上必须有「机器在开机」的可见状态**，否则用户会以为卡死了。

---

## 2. 其他产品的做法（用来反推哪些是共性）

| 产品 | 隔离 | 生命周期 | 出站 | 凭据 | 人怎么看进去 |
|---|---|---|---|---|---|
| **Muse** 🟢🟡 | CH microVM + nspawn cell + 独立 browser VM | 每用户常驻，rollout 换机 | MITM 代理 + eBPF + 污点 + 审批 | authd 代理，agent 永不可见 | 客户端接管浏览器、看文件 |
| **Manus** 🟢 | E2B 微虚拟机（Firecracker），每任务一台 | 任务级；另有付费「Cloud Computer」7×24 常驻 | 沙箱内自由联网 | 沙箱内 | 内置浏览器/VS Code/终端画面 |
| **Codex cloud** 🟢 | 容器（`codex-universal` 镜像） | 容器状态缓存 12h，setup 脚本 + maintenance 脚本 | **agent 阶段默认断网**，setup 阶段有网 | secrets 只给 setup，**进 agent 阶段前移除** | 结束给 diff + 终端日志 |
| **Claude Code cloud** 🟢 | 托管容器（Ubuntu 24.04），或自托管 runner；本地是 OS 级沙箱（seatbelt/bubblewrap） | 会话级 | 443 + MITM 网关 | 网关侧 | 终端输出 / PR |
| **Devin** 🟢 | blueprint → 构建 **snapshot** → 每个 session 从快照起 | 会话级 VM，可 SSH 进入 | 可配 | 环境变量/secret 注入 | 桌面 + 终端 + 浏览器 |
| **Docker Sandboxes (`sbx`)** 🟢 | 本地 microVM（免费、含商用）/ Docker 云的云沙箱（付费） | 本地/云各自生命周期 | 网络策略可配 | 凭据管理 + MCP 网关 | 可 SSH 接 VS Code/Cursor 🟢 |
| **microsandbox** 🟢 | libkrun 微虚拟机，OCI 镜像，fork/snapshot | 命名沙箱，可 detached 长跑 | 域名白名单 | 密钥不进 VM | SDK/CLI |

### 抽象出来的 6 条共性

1. **一任务/一用户一台可抛弃的机器**，没人拿共享容器跑不可信代码。
2. **镜像预热**是体验的全部：把 `pip install`、克隆仓库、装浏览器这些分钟级操作放到**镜像/快照构建期**，
   运行时只做「嫁接工作区 + 起进程」。Devin 的 snapshot、Codex 的 12h 缓存、Muse 的 pre-boot hatchling 都是同一招。
3. **默认收紧，按需放开**：Codex 直接给 agent 断网；Muse 给「干净流量」自动放行、有污点的才问。
4. **人必须能看进去**：终端流、浏览器画面、diff。看不到的自主执行没人敢开。
5. **凭据不进沙箱**（或至少沙箱内只拿 surrogate）。这条做得好的只有 Muse / Codex（部分）。
6. **必须有生命周期与配额**：闲置回收、并发上限、绝对超时、按秒计费。Muse 的 balloon、E2B 的 session 上限、
   Claude Code 的 idle reclaim 都是同一件事。

---

## 3. 技术选型：本地 / 自托管 / SaaS

### 3.1 你这台机器的硬约束（先说，因为它直接砍掉一半选项）

```
macOS 26.7.1 / Intel Core i7-9750H / 32GB
uname -m → x86_64（真 Intel，不是 Rosetta）
Docker Desktop 29.6.2，只分了 8GB 内存
container / sbx / msb 均未安装
```

- **没有 KVM**，也没有嵌套虚拟化 → Firecracker / Cloud Hypervisor / E2B Runtime **在本机跑不了**；
- **Apple Silicon 专属方案直接出局**：`apple/container`（每容器一台轻量 VM，需 Apple silicon）、
  microsandbox（README 明确写 macOS 要求 Apple Silicon）；
- 本机可用的「真隔离」只剩：**Docker Desktop 的 Linux VM 里跑容器**（隔离边界是那台 VM，容器之间仍共享内核）、
  或另装 **Lima/UTM** 起一台 QEMU/HVF 的 Linux VM 再在里面跑容器；
- Docker 只分到 8GB，跑「浏览器 + 编译」会紧。

结论：**本地开发用容器 + 云端/生产用微虚拟机**，通过 provider 抽象切换，是唯一不别扭的路径。
不要为了「本地也能叫 VM」去装 Virtualization.framework 那套——Intel Mac 上的收益远小于维护成本。

### 3.2 候选对比

| 方案 | 形态 | 自托管 | 本机开发 | 冷启动 | 价格 | 备注 |
|---|---|---|---|---|---|---|
| **E2B** | Firecracker 微虚拟机 | ✅ Apache-2.0，runtime/控制面全开源；单机 `docker compose up` 版本（embed/compose）或 Terraform 上云 | 只能连云 | ~几百 ms | $0.0504/vCPU·h + $0.0162/GiB·h；Hobby 20 并发/单沙箱 1h；Pro $150/mo→100 并发/24h；Enterprise $3,000/mo 起 | 生态最成熟，Manus 就在用；文档/模板体系完整 |
| **Cloudflare Sandbox SDK** | Containers（Workers Paid） | ❌（要在 CF 上） | 只能本地模拟 | 亚秒–2s | Workers Paid + 容器用量 | 有 Workers 就别扭；但 per-agent 沙箱 + 端口暴露 API 很顺 |
| **Docker Sandboxes `sbx`** | 本地 microVM / 云沙箱 | 本地免费 | ✅ 最顺 | 秒级 | 本地免费；云按量 | 面向前台 agent（Claude Code/Codex/Cursor），有 Kits/MCP 网关/网络策略/API+SDK；**但它是给 agent 用的产品，不是可嵌入的库** |
| **microsandbox** | libkrun 微虚拟机，Apache-2.0 | ✅ 可嵌入进程内 | ❌ Apple Silicon 才行 | <100ms（宣称） | 免费 | fork/snapshot/OCI 镜像/SDK 齐全；README 自承 **beta**，且已在换组织（superradcompany） |
| **Freestyle VMs** | 完整 VM，fork/pause | ❌ 托管 | 只能连云 | ~65ms（宣称） | 按量 | 支持嵌套虚拟化/GPU，适合「用户自带 dev 环境」 |
| **Daytona** | 容器/Kata/Windows/macOS | 曾有开源版 | — | — | — | ⚠️ **2026-06 起仓库已 ARCHIVED、核心转闭源**，社区 fork（Nightona）接手。不要基于它做长期依赖 |
| **Modal** | gVisor | ❌ | 只能连云 | ~1s | 按量 | 唯一能塞 GPU 的；跑 Python 负载顺手 |
| **自建 Firecracker/CH 编排** | 微虚拟机 | ✅ | 需 Linux+KVM | 自己优化 | 服务器成本 | 编排/镜像/网络/回收/多租户全靠自己 → 这是另一个项目，不是一个功能 |

成本量感：E2B 默认 2 vCPU/4 GiB = **$0.166/h**；一个用户每天用 1 小时 ≈ **$5/月**。
自托管一台 8c/32GB 的 KVM 机器（约 $40–60/月）超卖后能同时跑 10–20 个同类沙箱，
但要把编排、镜像分发、闲置回收、监控全部自己写。

### 3.3 建议

- **接口先抽象，后端可换**：`SandboxProvider`（create/exec/put_file/get_file/stop/snapshot），
  先实现 `docker`（本机）+ 预留 `e2b`（云）。
- **短期不要自建微虚拟机编排**。要自托管就上 E2B 的开源 runtime（它已经把 Firecracker 编排做完了）；
  自建编排只有在「这个功能本身就是研究课题」时才值得。
- **明确排除 Daytona**（已归档闭源）。

---

## 4. 对本项目的影响：这个功能会打破三个既有不变式

这比选哪家供应商重要得多。`app/tools/approval.py` 和 `app/services/chat_service.py` 的注释把当前架构讲得很清楚：

### ① 「无状态服务端」→ 引入服务端拥有的长生命周期资源

现在：历史由客户端每轮全量回放，服务端不保存会话，审批靠 `tool_call_id` 在客户端历史里重建。
沙箱一旦存在，就多出一份**服务端唯一知道**的状态：这台机器上有什么文件、跑着什么进程、还剩多少寿命。
客户端回放历史**不足以**重建它。这意味着：

- 需要新表（`sandbox`：id / user_id / provider / provider_ref / image / state / created_at / last_used_at / expires_at / quota）；
- 需要**归属与鉴权**：`(sandbox_id, user_id)` 双条件，跟现有删除接口的写法保持一致；
- 需要**回收路径**：闲置超时、绝对超时、用户主动销毁、进程崩溃后的孤儿清理。
  `main.py --worker` 已经是一个独立进程，reaper 放那里最自然（它已经是「可重启的长任务」形态）。

### ② SSE 单向 → 需要双向通道

终端、浏览器画面、远程桌面都要双向。当前全仓**没有任何 WebSocket**，只有 `EventSourceResponse`。
两条路：

- **A（省事）**：`exec` 走 HTTP 请求/响应，长命令返回增量输出靠轮询或「先 stream 到 DB 再读」；
  体验差但不动架构，P0 够用。
- **B（正解）**：加 `/api/sandbox/{id}/attach` 的 WebSocket，后端代理到沙箱（xterm.js / noVNC）。
  这条一旦要做，要考虑多 worker 部署下的连接亲和（不是禁止，但必须显式设计）。

### ③ 逐工具审批 → 必须换成「边界审批 + 沙箱内审计」

这是最容易做错的一点。现有 `RiskLevel` 的执行闸门是**逐次工具调用**的。
如果 `sandbox_exec` 标 `DANGEROUS`：模型每敲一条 `ls` 都要用户点一次，沙箱就失去意义。
如果标 `READ_ONLY`：等于把一台机器无审计地交出去（`guard.py` 那套失败隔离也管不到沙箱内部）。

可选模型（需要一条 ADR，不要默认选）：

| 模型 | 谁被审 | 体验 | 风险 |
|---|---|---|---|
| 逐命令审批（现状扩展） | 每次 exec | 不可用 | 低 |
| **边界审批** | 创建沙箱、出站域名、挂载/导出文件 | 顺滑 | 沙箱内破坏无感知 → **必须配审计日志** |
| 污点出站（Muse 式） | 只有「读过用户数据后要出网」的请求 | 最顺 | 实现成本最高（eBPF/数据流追踪），Python 侧做不了，得在沙箱镜像里做代理 |

务实的落地顺序：**边界审批 + 全量命令审计（可回放）** 起步；污点出站留作演进，不追求首版。

### ④ 还有三件容易被忽略的小事

- **能力探针**：`/api/capabilities` 是「按实际装配结果报给前端」的唯一权威（见 `app/api/capabilities.py`）。
  沙箱加进来后要在这里报 `sandbox=true/false` + 后端类型，否则空对话页的起步建议会推荐做不到的事。
- **计费口径：只有 token，沙箱不单独计价（2026-10-24 用户决定，取代本节原判断）**。
  沙箱的消耗最终只通过两条渠道露面：① `exec` 输出进上下文 → token；② 运维成本（节点池），那是运营账不是产品账。
  因此**不做**秒×资源的二级计费、不做套餐/付费门，注册登录即可用（§10.9）。
  反过来说：既然 token 是唯一成本面，`exec` 的输出截断就不是「优化」，而是**成本控制点**。
- **上下文预算**：`exec` 输出是典型的「一次 5000 行日志」。必须像 `tavily_search` 那样在工具内截断
  （头 + 尾 + 总行数 + 「完整输出在沙箱 `/path`」），否则一次 `npm install` 就把上下文打爆，
  触发 `context_compaction`——而压缩掉的恰恰可能是用户的需求。

---

## 5. 建议路线（最小可行 → 可演示 → 可扩展）

**P0（1–2 天，无 UI，纯工具）**
`SandboxProvider` 协议 + `docker` 后端（`python:3.12-slim` 之类，只读挂载工作区，出站走 `--network none` 或白名单）；
工具 `sandbox_exec` / `sandbox_write_file` / `sandbox_read_file`；
租约绑定会话（`conversation_id`），每轮续期，闲置回收；`enabled_checker` 读配置，默认关。
验收：让模型「把这个 CSV 清洗完画个图」，它自己写脚本、跑、把产物路径回报。
**到这一步就已经有真实价值了，而且不碰任何前端。**

**P1（3–5 天，可演示）**
前端侧边栏 + xterm.js 终端（WebSocket 或轮询）、沙箱状态卡（创建中/运行中/已回收）、
审计日志（每条命令 + 退出码 + 截断输出）、配额与「一键销毁」、`capabilities` 上报。
验收：用户能实时看见 agent 在做什么，并随时接管。

**P2（按需）**
换/加云端 provider（E2B 或自托管 runtime）；镜像预热（把依赖装进模板，冷启动从分钟级降到秒级）；
snapshot/fork（「同样的环境连开三条路试试」）；出站策略细化（域名审批 + 凭据代理）。

**明确不做**：自建 Firecracker 编排、Windows/macOS 沙箱、GPU 透传、VNC 桌面（除非有明确需求）。

---

## 6. 风险清单

1. **成本失控**：忘关的沙箱按秒烧钱。必须有绝对超时（硬上限）+ 闲置回收 + 每用户并发上限 + 日预算熔断。
2. **供应链**：沙箱镜像里装的包、agent 自己 `pip install` 的东西。至少固定基础镜像 digest，别用 `latest`。
3. **凭据泄漏**：如果沙箱里能读到 `.env`（本项目根目录就有一份 15KB 的），一次 `cat` 就全出去了。
   挂载必须白名单，且 `provide_sandbox` 时不得继承后端环境变量。
4. **审计缺失**：边界审批的代价是内部不可见。没有审计日志的边界审批 = 没有审批。
5. **「看起来安全」**：容器共享内核。营销/文档里不要写成「虚拟机」，否则误导使用者以为隔离等级更高。

---

## 7. 待定（见本轮提问）

1. 沙箱给谁用：只给 agent 跑代码，还是用户也要进去操作（终端/桌面/浏览器）？
2. 部署形态：只本机单人，还是多用户线上服务？（决定隔离等级与配额的必要性）
3. 生命周期：每任务新机器（用完即焚）/ 每用户常驻 / 任务内跨轮次但结束销毁？
4. 出站：自由联网 / 白名单代理 / 默认断网按域名审批？

---

## 8. 决策记录与 P0 设计（2026-10-24 定）

### 8.1 四条决定

| 维度 | 决定 | 已知代价（不是「以后再说」，是现在就接受的） |
|---|---|---|
| 用途 | **只给 agent 跑代码 / 处理文件**，不做终端共享、不做浏览器 | 不需要 WebSocket、不需要 noVNC、不需要连接亲和；但「看得进去」只剩日志与产物文件，P1 才有 UI |
| 部署 | **本机单人用**，`SandboxProvider` 留出云端口子 | 隔离等级是**容器**（共享内核）。文档/README 里不准写成「虚拟机」，否则读者会误判隔离强度。「单人」只描述部署规模，**不降低代码要求**：认证层（注册/三级角色/跨设备同步）本来就是多用户，越权检查一条不能少（见 8.7） |
| 生命周期 | **任务内跨轮次，结束即销毁** | 中间状态只靠文件系统——`docker exec` 每次是新进程，**没有持久 shell 会话**（cwd/环境变量不保留）。这点必须写进工具描述，否则模型会假设 `cd` 生效 |
| 出站 | **自由联网** | 这是四条里风险最高的一条，见 8.3。换来的是模型能自己 `pip install`、抓数据、跑 `git clone`，不用每次找我们开白名单 |

### 8.2 关键实现约束（调研时发现的，直接影响设计）

1. **`RunContext[None]`：工具里拿不到 user_id，也拿不到会话 id。**
   全仓工具签名是 `ctx: RunContext[None]`（如 `app/tools/tavily_search.py:92`），
   而 `ChatRequest`（`app/schemas/chat.py:190`）**没有 session 字段**——会话 id 是客户端 `sess_<uuid>`，
   只在同步接口里出现。
   → 结论：**不要为了沙箱去改 deps 类型**（那是三处 `agent.run` + 缓存键的连锁改动）。
   身份走 ContextVar（`knowledge_scope` 同款）。但 `session_id` **必须**进 `ChatRequest`——
   「每会话一台机器」要求服务端能确定是哪个会话（见设计文档 §6），这两件事不矛盾：
   deps 管「谁」，`session_id` 管「哪一个会话」，都不是授权依据。
   P0 用 «**不透明句柄**»：`sandbox_create` 返回 `sandbox_id`，后续工具显式带上它；
   `sandbox_id` 天然活在客户端回放的对话历史里，和现有「服务端不保存会话」的模型对齐。
   **但句柄是客户端可控的输入，不是凭据**——授权见 8.7，身份由服务端注入（`knowledge_scope` 同款）。
   服务端兜底：`sandbox_id` 缺省时取「**该用户**最近活跃的 lease」，没有就返回「先 create」。

2. **审批闸门可以不动。** `RiskLevel` 是逐次调用的执行闸门，但「沙箱内的命令」不是它的对象——
   把 `sandbox_create` 标 `SAFE_WRITE`（用户点**一次**「给这台机器开个沙箱」），
   `exec / write_file / read_file` 标 `READ_ONLY`（沙箱内可弃、可撤销，符合 `approval.py` 的
   「分界线是能不能撤销」），`sandbox_destroy` 标 `READ_ONLY`（销毁反而是安全动作）。
   这样**不需要第二条 ADR**，风险也真的落在边界上：机器由谁开、开了之后能看见什么。

3. **回收必须能扛进程崩溃。** `main.py --worker`（已有独立进程）跑 reaper：
   `expires_at < now()` 的租约一律 `docker rm -f`；另外启动时按容器 label 清理孤儿
   （进程被杀时 DB 里的租约还在，容器也还在）。

4. **exec 输出必须截断。** 一次 `npm install` 就能把上下文打爆，触发 `context_compaction`，
   而那恰好会压掉用户需求。规则：头 4000 + 尾 2000 字符 + 「共 N 行 / 完整输出见 `/tmp/xx.log`」。

### 8.3 「自由联网」的补偿控制（P0 必须做，不能只写文档）

自由联网的真实风险不是逃逸（容器逃逸另说），而是**上下文里的敏感内容被写进沙箱再发出去**：
模型完全可以把用户上传的原件、知识库检索结果写成文件再 `curl` 走。所以：

- 沙箱**不挂载任何宿主目录**（P0 完全不挂，文件只能由 `sandbox_write_file` 显式写入）；
- 沙箱**不继承后端环境变量**（不许 `-e` 透传，`docker run` 只给 `--env LANG` 这类无语义变量）；
- 宿主 `.env`（15KB，含网关 key）在容器内不可达——这是靠「不挂载」而不是靠路径过滤实现的；
- 所有命令进审计表（`sandbox_command`：lease_id / seq / command / exit_code / 截断输出 / 耗时），
  资产顺序是「先有审计，再谈放开」。

### 8.4 P0 文件清单（`app/services/sandbox/` 新包）

| 文件 | 内容 | 备注 |
|---|---|---|
| `app/services/sandbox/base.py` | `SandboxProvider` Protocol、`SandboxSpec`、`SandboxExecResult` | 只有 6 个方法：create / exec / put_file / get_file / destroy / exists；**后端可换是唯一的抽象目标** |
| `app/services/sandbox/docker.py` | 用 `docker` CLI 走 `subprocess`（`run -d` / `exec` / `cp` / `rm -f`） | 不引新依赖；同步调用放 `asyncio.to_thread`，和 `app/services/storage/s3.py` 处理 boto3 的方式一致 |
| `app/services/sandbox/lease.py` | 租约 CRUD + reaper + 审计写入；**所有读写都带 `WHERE user_id = :uid`** | 表：`sandbox_lease`（`id` = `sbx_<uuid>`、`user_id` 非空、`container_name` 只由服务端生成）/ `sandbox_command` |
| `app/tools/sandbox_scope.py` | 身份作用域 ContextVar，对标 `app/tools/knowledge_scope.py` | 与 `knowledge_scope` 分开是仓库既有约定（缓存隔离 vs 授权边界，不得已才共用一个 Var） |
| `app/tools/sandbox.py` | 5 个 `ManagedTool`：`sandbox_create` / `sandbox_exec` / `sandbox_write_file` / `sandbox_read_file` / `sandbox_destroy` | `enabled_checker` 读 `SANDBOX_ENABLED`，**默认关** |
| `docker/sandbox.Dockerfile` | 基础镜像：python3.12 + pandas/matplotlib/requests/pillow + ripgrep + git | 预热的第一层（P2 才做模板快照） |
| `migrations/versions/*_sandbox.py` | 两张表 + 索引 `(user_id, state, expires_at)` | 迁移不是可选项，见 README |
| `tests/test_sandbox_tools.py` | 用 `FakeProvider` 测工具契约：输出截断、缺 sandbox_id 兜底、销毁幂等 | 不依赖 Docker |
| `tests/test_sandbox_lease.py` | 租约过期、reaper 选行、审计序号 | |

配置（`app/config/settings.py` 追加，`.env.example` 同步）：
`SANDBOX_ENABLED`(false) / `SANDBOX_BACKEND`(docker) / `SANDBOX_IMAGE` / `SANDBOX_MEMORY`(2g) /
`SANDBOX_CPUS`(2) / `SANDBOX_IDLE_SECONDS`(900) / `SANDBOX_MAX_SECONDS`(7200) / `SANDBOX_NETWORK`(bridge)。

容器硬化（`docker run` 参数，P0 就带上）：
`--cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --memory/--cpus --user 1000:1000`，
label `bennett.sandbox=1` + `bennett.lease=<id>`（孤儿清理靠它），命名 `bennett-sbx-<8位>`。

`app/api/capabilities.py` 追加 `sandbox: bool`（沿用「按实际装配结果上报」的原则）。

### 8.5 验收（P0 的 Definition of Done）

1. `SANDBOX_ENABLED=false` 时：工具不出现，`capabilities.sandbox=false`，既有测试全绿；
2. 打开后：「把这个 CSV 清洗完画张图」→ 模型自己 `sandbox_write_file` → `sandbox_exec` 跑 pandas →
   `sandbox_read_file` 取回产物路径，**全程不需要 WebSocket**；
3. 人为 `kill -9` worker 后重启：孤儿容器被 label 清理干净，DB 里租约状态可解释；
4. `exec` 输出 100KB 时，模型只看到头尾 + 行数，上下文用量不爆；
5. **拿 B 账号带着 A 的 `sandbox_id` 调任何工具，回复与「随便编一个 id」完全一样**，且不返回 A 的任何数据（见 8.7 的五个用例）。

### 8.7 跨用户越权：威胁模型与强制点（**硬要求**）

`sandbox_id` 出现在对话历史里，而对话历史是**客户端回放**的——它在信任边界外侧，攻击者完全可控。
所以「换个 ID 就能进别人的机器」不是疏忽，是默认结果，必须专门堵。

**本项目里这不是理论路径**：仓库支持「导出为 Markdown」与「多会话合并归档」，
`sbx_...` 会跟着正文在两个用户之间真实流动（导出→发给别人→导入、或者直接粘进另一个会话）。
换句话说，`sandbox_id` 会以文字形式跨用户传播，它是**公开信息的强度**，不能当凭据用。

| 攻击 | 在本仓库里怎么发生 | 强制点 |
|---|---|---|
| 拿别人的 id 读文件/跑命令 | 导出/粘贴/直接改请求体历史 | `resolve_lease(lease_id, user_id)`：`WHERE id = :id AND user_id = :uid AND state='running'`。查不到就是查不到 |
| 拿别人的 id 销毁机器 | 同上 | destroy 同样走 owner-scoped 查询（写操作不豁免） |
| 靠错误文案当探测巫 | 试不同 id 看回应差异 | **not-found 与 not-yours 返回字节级相同的文案**：「沙箱不存在或已回收，请重新创建」。不说「无权访问」，不说「属于其他用户」 |
| 枚举 id | 脚本狂发 | `sbx_<uuid4 hex>`（122 bit）+ 复用现有 `enforce_chat_rate_limit` + `denied` 审计行 |
| 「最近活跃租约」兑底跨人了 | `sandbox_id` 缺省时服务端自己找 | 兑底语句**必须**带 `user_id`：`WHERE user_id=:uid AND state='running' ORDER BY last_used_at DESC LIMIT 1`。这是最容易写完就泄的一行 |
| 模型直接指定容器名 | 工具参数把 `container` 传进 docker CLI | 容器的名字/ID **只能来自 DB 行**；模型给的 `sandbox_id` 先过 `^sbx_[0-9a-f]{32}$` 正则，再查库。argv 列表调用（不经宿主 shell），模型给的命令只作为 `bash -lc` 的**单个参数** |
| DB 脏数据/回滚/写错行 | 迁移失败、手动改库、并发 bug | 第二道独立校验：`docker inspect` 读 label `bennett.owner`，必须等于 `sha256(str(user_id))[:16]`，不等则拒绝 + 告警日志。DB 说可以不算可以，label 也说可以才算 |
| 审计表越权读 | P1 的审计 UI/接口 | 一律 `join sandbox_lease on lease_id` 并带 `user_id`；不许出现能按 lease_id 裸查的接口 |
| 超管顺手看一眼 | 已有三级角色 | **P0 明确不允许**：任何角色都不能 exec 进别人的沙箱。日后真要，必须是带 actor 的双人可审计流程，不是默认权限 |

身份怎么进到工具里（**不要新造轮子**）：仓库已有现成答案——`app/tools/knowledge_scope.py` 的注释已经把话说完了：
「凡是「身份」这种用户不能自己声明的输入，都必须由服务端在调用前注入」。
照搬：`stream_chat_events` 里 `knowledge_scope(user_id)` 旁边加 `sandbox_scope(user_id)`，
工具里读 `current_sandbox_scope()`；为 `None`（CLI / 未登录路径）时直接回「当前会话没有关联用户，无法使用沙箱」，
和知识库工具的行为一致。**不引入 deps、不改 `ChatRequest`、不加 session 字段**。

必须有的测试（少一条就算没做完）：

1. `test_sandbox_cross_user_denied`：A 建沙箱 → B 拿着 A 的 `sandbox_id` 调 `exec` → 返回与「假 id」**逐字节相同**的文案，
   且响应里不包含 A 的任何文件名/内容；
2. `test_sandbox_cross_user_destroy_denied`：B 销毁 A 的沙箱 → 拦下，A 的租约仍为 `running`；
3. `test_sandbox_fallback_is_user_scoped`：B 不传 `sandbox_id` → 兑底不能命中 A 的租约；
4. `test_sandbox_label_mismatch_denied`：DB 行指向一个 label 不符的容器 → 拒绝且不执行；
5. `test_sandbox_scope_absent`：无 scope 时工具不起 docker 调用（用假 provider 断言）。

### 8.6 明确留到后面

P1：xterm.js 面板 / 产物文件下载 / 会话级「沙箱卡」/ 上传件投喂（复用 `uploads`）。
P2：`e2b` provider（云）/ 镜像预热 / snapshot-fork / 出站白名单与代理 / 污点出站（Muse 那一套在
Python 侧做不了，得在沙箱镜像里做 eBPF 或 L7 代理，属于独立课题）。

---

## 9. 实现方案（选定）

### 9.1 一句话

**本机用 Docker 容器（provider=`docker`），真正的虚拟机留到有 KVM 的 Linux 机器上用 E2B 的开源 runtime（provider=`e2b`）；自己写 Firecracker 编排不做。**

### 9.2 为什么本机是容器而不是虚拟机（这是环境决定的，不是偏好）

| | 与 macOS 的边界 | 容器之间的边界 | 本机可行性 |
|---|---|---|---|
| Docker Desktop 容器 | hypervisor（Desktop 自带一台 LinuxKit VM） | **共享内核** | ✅ 现在就有 |
| Lima/Colima 起的 Linux VM 里再跑容器 | hypervisor | 共享那台 VM 的内核 | ✅ 需另装，`DOCKER_HOST` 指过去即可，**provider 代码不用改** |
| Firecracker / Cloud Hypervisor | — | 独立内核 | ❌ Intel Mac 无 KVM、无嵌套虚拟化 |
| apple/container、microsandbox | — | 独立内核 | ❌ 仅 Apple Silicon |

关键判断：**本机场景下"共享内核"没那么可怕**——容器和 macOS 之间已经有 hypervisor 边界，共享内核的是你自己的进程。
"别人的数据"这个威胁只有在**多租户**时才成立，而多租户本来就不该跑在你的笔记本上。
所以顺序是：本机用容器把功能和授权做对 → 上云换 microVM，而不是在笔记本上死磕真虚拟机。

### 9.3 provider 契约（唯一的抽象目标：后端可换）

```python
class SandboxProvider(Protocol):
    async def create(self, spec: SandboxSpec) -> SandboxHandle: ...   # 返回 provider_ref + 就绪时间
    async def exec(self, ref: str, cmd: str, timeout_s: int, workdir: str) -> ExecResult: ...
    async def put_file(self, ref: str, path: str, data: bytes) -> None: ...
    async def get_file(self, ref: str, path: str, max_bytes: int) -> bytes: ...
    async def destroy(self, ref: str) -> None: ...                    # 幂等
```

- `SandboxHandle.ref` 对 docker 是**容器名**（服务端生成，模型永远看不到也传不进），对 E2B 是它自己的 `sandbox_id`。
- 上层（`lease.py` / `tools/sandbox.py`）只认 `sbx_<32hex>` 这个租约 id，其余全是 provider 内部细节。

### 9.4 docker 后端的关键参数（P0 就按这个写）

```bash
docker run -d --name bennett-sbx-<8> \
  --label bennett.sandbox=1 --label bennett.lease=<lease_id> --label bennett.owner=<sha256(uid)[:16]> \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 \
  --memory 2g --cpus 2 --user 1000:1000 \
  --network bridge --tmpfs /tmp:rw,size=256m \
  -w /home/sandbox/workspace bennett-sandbox:0.1 sleep infinity
```

- `exec`：`docker exec -w <dir> <name> bash -lc "<cmd>"`——**命令超时靠容器内的 `timeout -k 5 Ns`**，
  因为 `docker exec` 超时后我们无法可靠杀掉远端进程；退出码 124 映射成「命令超时，请缩小范围」。
- `put_file`：写本地临时文件 → `docker cp`；`get_file`：`docker cp <name>:<path> -` 拿到 tar 流再用 `tarfile` 解析。
- 不挂载宿主目录、不透传 `-e`（见 8.3）。
- 需要 `docker.sock` → 意味着 web 进程握有宿主 root 等价权限。**本机可接受，但要写进文档**；
  上云换成 `e2b` provider 后这个特权面直接消失。（真要收紧就上 `docker-socket-proxy` 或独立小服务，P2。）

### 9.5 换到 E2B 时的映射（P2，不改上层）

| 我们的方法 | E2B SDK（接之前按当前版本文档核对） |
|---|---|
| `create` | `Sandbox.create(template="bennett-sandbox")` |
| `exec` | `sbx.commands.run(cmd, timeout=...)` |
| `put_file` / `get_file` | `sbx.files.write(...)` / `sbx.files.read(...)` |
| `destroy` | `sbx.kill()`；闲置用 `sbx.pause()` 省内存 |

价格：$0.0504/vCPU·h + $0.0162/GiB·h → 默认 2c/4G ≈ **$0.166/h**，一人一天 1 小时 ≈ **$5/月**。
自托管则用 E2B 开源 runtime（Apache-2.0，单机 `docker compose up` 版本或 Terraform），需要一台有 KVM 的 Linux。

### 9.6 不选的，以及为什么

- **Daytona**：2026-06 起仓库归档、核心转闭源，长期依赖风险不可接受。
- **Docker Sandboxes (`sbx`)**：它是"给你跑 Claude Code/Codex 的产品"，不是可嵌入的库；生命周期由它管，我们拿不到 per-lease 控制。
- **microsandbox / apple/container**：Apple Silicon 专属，这台是 Intel。
- **自建 Firecracker 编排**：镜像分发、快照、网络、孤儿回收、多租户全自己写——那是另一个项目。
- **Modal / Cloudflare**：能用，但一个绑 Python 生态、一个绑 Workers，都不如 E2B 中立在"可自托管"这一点上。

---

## 10. 企业级方案（**最终**，取代 §8.1 的「本机单人」与 §9 的本地容器结论）

### 10.1 目标不变式

1. 部署上线后，**每个用户 × 每个会话**一台专属机器，互不可见（不是「一个用户一台」，也不是「全局共享池里随便给一台」）。
2. 机器**不给用户点按钮**才有：第一次需要执行时自动创建（用户注册/开新会话后首次用到即建），前端看到「正在开机」。
3. **没有心跳就销毁**：心跳断了一定时长 → 自动回收，不需要用户点关闭，不依赖客户端配合。
4. 跨租户隔离是 hypervisor 级：共享内核容器在这一档**不合格**（§10.4 解释）。

### 10.2 拓扑：控制面 / 数据面必须分机

```
浏览器 ──心跳/SSE──> FastAPI 控制面（有 DB 凭据、.env、LLM key）
                        │  HTTP + API token
                        ▼
                  Sandbox 编排面（E2B Runtime / 自建节点池）
                        │  Firecracker / Cloud Hypervisor
                        ▼
             ┌──────────── 每会话一台 microVM ────────────┐
             │ 独立内核 · 独立网络命名空间 · 出站走代理      │
             └───────────────────────────────────────────┘
```

硬规则：**控制面绝不在跑用户代码的那台机器上**。控制面持有 DB 凭据、LLM 网关 key、用户密文，
一旦和工作负载同机，容器逃逸直接等于全站数据泄露。§9 里「web 进程挂 docker.sock」那套只配留在本机开发。

### 10.3 生命周期：两个时钟 + 心跳

状态机：`provisioning → ready → (paused) → destroying → destroyed`

| 时钟 | 默认 | 语义 | 到点动作 |
|---|---|---|---|
| `idle_ttl` | 10 min | `max(last_heartbeat, last_activity)` 距今超过它 | 销毁（**这是「没心跳就销毁」**） |
| `hard_ttl` | 4 h | 创建至今 | 销毁（防止「一直有心跳」的僵尸会话无限烧钱） |

- **心跳来源有两个，取较大值**：
  - 客户端：会话页面在前台且 SSE 连着时，每 30s `POST /api/sandbox/{lease_id}/heartbeat`；
  - 服务端：每次 `exec` / 文件读写都 `last_activity = now()`（兜住「用户不在看、但 agent 还在跑长任务」）。
- **只信客户端心跳会出事**：标签页挂着不关 = 心跳不断 = 机器永远活着。所以必须有 `hard_ttl`，
  以及「前台可见」判定（页面 hidden 时降频或停发，让 `idle_ttl` 把它收掉）。
- 销毁是 `destroy`（幂等）+ 清理 provider 侧资源；`paused` 只在 provider 支持 `pause` 时使用（省内存，但仍计时）。
- **销毁前必须先保产物**：把 workspace 打包上传对象存储（本项目已有 RustFS/S3），`lease.artifact_uri` 记下来。
  「按会话销毁」+「用户回来东西没了」是产品级事故，不是技术细节。

### 10.4 为什么这一档不能再用容器

| | 容器 | microVM |
|---|---|---|
| 两个租户之间的边界 | 内核（seccomp/caps/namespace 是**同一内核**的配置） | hypervisor |
| 内核 0day | 跨租户 | 需先打穿 hypervisor |
| 侧信道/内核态攻击面 | 共享 page cache、共享内核版本 | 基本不共享 |
| 合规叙事 | 「我们做了容器加固」 | 「每会话独立内核」 |

单人自用可以接受容器；**企业多租户不行**——你没法向客户解释「A 和 B 的内核是同一个」。

### 10.5 用什么服务（结论）

**主选：自托管 E2B Runtime（Apache-2.0）**，跑在自有 KVM 节点上。
理由：Firecracker microVM + 快照恢复 + pause/resume + 镜像模板 + 编排 API 全都有，
Manus 这类产品在生产验证过；而且**开源可自托管**，数据不出自己的机房 —— 企业招标时的第一个问题就是这个。

| 方案 | 多租户隔离 | 数据是否出机房 | 上线速度 | 结论 |
|---|---|---|---|---|
| **E2B Runtime 自托管** | microVM | 否 | 中（要一台 KVM 机器） | ✅ 主选 |
| E2B Cloud（含 BYOC/Enterprise） | microVM | 是（BYOC 可谈） | 快 | 备选：先上云再迁自托管，SDK 形状一致 |
| 自建 Firecracker/CH 编排 | microVM | 否 | 很慢 | ❌ 另一个项目 |
| K8s + Kata / gVisor | microVM / 用户态内核 | 否 | 中偏慢 | 🟡 若已有 K8s 集群才考虑，否则运维面更大 |
| Cloudflare Containers / Fly Machines / Modal | 各家不同 | 是 | 快 | 🟡 绑定性强，且都不是「自家机房」 |
| 共享内核容器（含 Docker/k8s 默认） | ❌ | 否 | 快 | ❌ 企业多租户不合格 |

容量与成本量感（2 vCPU / 4 GiB per 会话）：16c/64GB 的 KVM 裸金属 ≈ $50–70/月，
内存上限决定并发约 10–12 台；同样负载放在 E2B Cloud（$0.166/h）≈ 每天 100 会话 × 2h ≈ **$1000/月**。
→ **日活会话过十来个小时就该自托管**，这基本是上线第一周就到的事。

### 10.6 控制面需要新增的东西（企业级不许省）

| 项 | 内容 |
|---|---|
| 授权 | 三元组 `(lease_id, user_id, session_id)`，全部由服务端解析；`session_id` 必须属于该用户（§8.7 的中立错误文案规则照旧） |
| 容量保护（**不是计费**） | 每用户并发会话机数 = 3、全局节点池上限 N、单机档位 2c4g；超限 429 + 明确文案，**不静默排队**（排队到超时是最差的体验）。注册即可用，这里挡的是「一个账号把池子打空导致所有人不可用」，不是付费分级 |
| 计量 | **只有 token**（沿用 `app/services/pricing.py` 与 usage 链路），沙箱不建独立账本；`sandbox_command` 只做审计与排障，不参与任何计费计算 |
| 审计 | 每条 `exec` 一条记录（命令、退出码、截断输出、耗时、来源 user/session），可回放 |
| 预热池 | 从模板快照恢复 ≈ 数百 ms；不做池化则「注册后第一次使用」要等冷启动（Muse 实测 13–40s），必须有「正在开机」状态 |
| 镜像版本 | 模板按版本号发布、可回滚；用户代码跑在哪个版本要可查 |
| 出站 | 企业档默认**白名单代理**（§8.3 那三条补偿控制 + 域名审批），不再沿用「自由联网」 |
| 可观测 | 每会话 CPU/内存/磁盘/网络采样，异常（OOM、盘满、代理 5xx）进告警 |

### 10.7 开发期怎么跑（不牺牲上面的东西）

- provider 契约不变（§9.3），`docker` 实现**只用于本地开发与 CI**：跑那 5 条越权测试和工具契约测试，不需要 KVM。
- 联调走 `e2b` provider 连测试节点/测试云环境；CI 里禁止用 docker 后端冒充生产行为（网络、快照、冷启动都不一样）。
- 部署形态：控制面任意容器平台；数据面是**独立节点池**（自有 KVM 或 E2B Cloud），两者用 API token 通信。

### 10.8 本轮决定对代码的直接影响（相对 §8.4）

1. `ChatRequest` 增加 `session_id`（可选，但沙箱工具要求存在）；校验规则：会话行存在则必须属于该用户，不存在则视为新会话。
2. 新端点 `POST /api/sandbox/{lease_id}/heartbeat`（owner-scoped，`sandbox_scope` 之外再加一道 DB 校验）。
3. `sandbox_lease` 唯一约束：`(user_id, session_id) WHERE state='running'` —— 一个会话最多一台。
4. reaper 从「本机 worker 顺手清」升级为**独立循环 + 告警**：销毁失败要重试并上报，不能静默吞掉。
5. 追加一张表：`sandbox_command`（审计 + 排障）；销毁流程包含「产物上传 → 记 uri → 才允许 destroy」。**不建 `sandbox_usage`**（§10.9）。

---

## 11. 「用什么框架创建虚拟机」——分层回答

### 11.1 一句话

**虚拟化基元是 Firecracker（KVM microVM，Rust）；创建/编排这一层用 E2B Runtime（Go，Apache-2.0）；我们自己的进程里没有任何虚拟化代码，只是 HTTP 客户端。**

### 11.2 分层

| 层 | 用什么 | 谁在用 | 我们是否直接接触 |
|---|---|---|---|
| 虚拟化基元（真正「创建 VM」的那个） | **Firecracker**：KVM 上的 Rust VMM，配套 `jailer`（chroot + cgroup + seccomp + netns + 非 root uid） | E2B Runtime | ❌ 不直接调 |
| 编排层 | **E2B Runtime**：api server（控制面）+ per-node orchestrator（驱动 Firecracker）+ template manager + `envd`（VM 内 agent） | 我们（自托管部署） | ❌ 不 fork |
| 对外接口 | orchestrator/api 的 **HTTP API** | FastAPI 控制面 | ✅ 只调这一层（§9.3 的 provider 契约） |
| 宿主 ↔ VM 内部 | **vsock** + gRPC（`envd`），不走网络栈 | orchestrator | ❌ |
| 镜像/模板 | 我们的 template：rootfs + 内存快照，版本化、可回滚 | template manager | ✅ 要维护 |
| 网络 | 每 VM 一个 tap + 独立 netns，出站过代理 | orchestrator + 我们的策略 | 半接触（策略在我们侧） |

关键点：**Firecracker 没有任何「库」给你在 Python 进程里 create_vm()**。它只有一个跑在 unix socket 上的 HTTP API
（`/firecracker.sock`），进程本身是宿主上独立启动的 VMM；这就是为什么必须有 orchestrator，
也是为什么「自己写编排」不是一个下午的事。

### 11.3 「创建一台 VM」实际发生的序列

冷启动（首次，或用新模板时）：

1. 分配 rootfs：从模板 **CoW / reflink**（XFS reflink 或 btrfs），不复制整块盘 —— 复制 4GB 就把 200ms 变成 10s；
2. 起 `firecracker` 进程（在 `jailer` 里，独立的 chroot/cgroup/seccomp）；
3. 依次 `PUT /machine-config`（vcpu/mem）、`/boot-source`（vmlinux + kernel args）、
   `/drives/{id}`（rootfs 只读 + 可写层）、`/network-interfaces/{id}`（tap）、`/vsock`；
4. `ACTION InstanceStart`；
5. guest 里的 `envd` 通过 vsock 报到 → orchestrator 标记 ready。

快照恢复（生产常态，快照预热的意义所在）：

1. `PUT /snapshot/load`（state file + mem file，可配 UFFD 惰性加载）+ `resume`；
2. 同一套 CoW 换掉可写盘 → 直接跳到第 5 步。

→ 前者秒级、后者百毫秒级。**用户「注册后第一次使用」的体验完全取决于模板与快照做得怎么样**，
这就是 §2 里「镜像预热是体验的全部」的那条共性在生产上的具体形态。

### 11.4 为什么是 Firecracker，不是别的 VMM

| | Firecracker | Cloud Hypervisor（Muse 用的） | QEMU | libkrun |
|---|---|---|---|---|
| 设备面 | 极少（无 PCI，virtio-mmio）→ 攻击面最小 | 全 PCI、热插拔、virtio-fs、GPU 直通 | 最全 | 中等 |
| 自带沙箱 | ✅ `jailer`（seccomp/cgroup/chroot） | ❌ 要自己做 | ❌ | 部分 |
| 快照/恢复 | ✅ 成熟（Lambda/Fargate 验证） | ✅ | ✅ 但重 | 有限 |
| 定位 | 「只跑短命不可信负载」 | 「能当正经虚拟机用」 | 通用 | 嵌入/本地 |

Fake 需求匹配：我们要的正是「短命、海量、不可信」——Firecracker 就是为此生的。
只有当后面要 **GPU 直通 / PCI 设备 / 虚拟桌面** 时才需要换成 Cloud Hypervisor，
那是产品形态变了，不是我们现在要解决的问题。

### 11.5 明确不用的

- **libvirt / virsh**：为长生命周期传统 VM 设计，状态在 `libvirtd` 里、XML 配置、启动慢，
  和「一会话一台、没心跳就死」的生命周期模型反着来。
- **QEMU 直接起**：能跑，但设备面太大、没有 jailer 级封装、内存开销高。
- **Docker / containerd**：共享内核，企业多租户不合格（§10.4）。本地开发才用。
- **自己写 Firecracker 编排**：tap/CoW/jailer/进程表/vsock/快照分发/孤儿回收，能做，
  但那是半个基础设施团队的工作量。E2B Runtime 已经把这些做完了，且开源可退。

### 11.6 如果一定要「不用 E2B」

最小可行路径是**自己写一个单节点 orchestrator**（约几百行 Go/Python）：
管 KVM 设备节点、tap + netns、CoW 盘、`jailer` 启动、vsock 端口分配、进程表 + 孤儿清理、快照分发。
这条路可行，但要清楚代价：**多租户隔离的正确性、快照兼容性、镜像分发、并发与回收的竞态** 全在你自己身上，
而这些恰好是不出错时没人看见、出错时直接跨租户泄露的部分。

---

## 10.9 准入与计量（**最终**）

**准入：注册登录即可用。** 没有套餐、没有付费门、没有沙箱专项额度、不做价格审核。
`/api/capabilities` 照样上报 `sandbox=true`，前端不区分「有权限/没权限」——
唯一的前置条件是「已登录」，和知识库、联网搜索目前的准入方式一致。

**计量：只有 token。** 沿用现有 `app/services/pricing.py` + usage 链路，沙箱**不建第二本账**。

这个判断在架构上是自洽的，不是省事：沙箱的成本只通过一条通路进入产品——`exec` 的输出进上下文变成 token。
所以：

- 沙箱不产生额外计价对象 → 不需要 `sandbox_usage`、不需要秒×资源的换算、不需要单价表；
- 反过来，**`exec` 的输出截断是唯一的成本控制点**：头 4000 + 尾 2000 + 「共 N 行，完整输出在 `/tmp/x.log`」。
  放开这一步，一次 `npm install` 就能把一轮对话的 token 成本推到几十倍，而账单上看起来只是「用户话很多」；
- 审计表 `sandbox_command` 只用于排障与合规，不参与任何金额计算——避免两本账口径不一致。

**唯一保留的限制是容量保护，不是付费分级：**

| 限制 | 值 | 拦住的是什么 |
|---|---|---|
| 每用户并发会话机 | 3 | 一个账号开 50 个标签页把节点池打空 |
| 全局并发 | 节点池容量 N（内存定，16c/64G ≈ 10–12） | 容量耗尽时**所有人**不可用 |
| 超限行为 | 429 + 明确文案（「当前机器资源已满，请稍后再试 / 结束其他会话」） | 不许静默排队到超时——那比拒绝更糟 |
| `idle_ttl` / `hard_ttl` | 10 min / 4 h | 忘关的会话无限占机（和计费无关，是资源卫生） |

这三条都不是「收费门槛」，是为了让「注册即可用」这个承诺真的守得住：
没有上限的免费资源，上线第一天就会变成不可用。数值放在 settings 里，运维可调。
