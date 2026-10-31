---
title: 权限与审批
description: 输入框里的权限选择如何落到执行闸门：权限模型、预设与自定义、无状态续跑、行业方案对照与安全边界。
---

# 权限与审批

审批解决的是「模型自作主张」：工具一旦执行，副作用就发生了。此前的做法是**每一次**写入或执行都停下来问一次，安全但啰嗦——用户很快会形成「无脑点批准」的肌肉记忆，审批反而失去意义。

这一版在输入框里加了一个权限选择器：用户先表达「本会话允许 Agent 做什么」，Agent 执行时只在越出授权范围时打断。审批从「每个动作一次」变成「一次意图表达 + 边界处的例外」。

## 先看行业怎么做

三类产品的取舍可以作为参照：

| 产品 | 机制 | 关键取舍 |
|---|---|---|
| Claude Code | 会话级 permission mode（manual / acceptEdits / plan / auto / dontAsk / bypassPermissions）+ allow / ask / deny 规则 | 模式决定基线，规则叠加；deny 规则在所有模式下都生效，allow 规则不能穿透 deny |
| Cursor | Run mode（Auto-review / Allowlist / Run Everything）+ `permissions.json` 自然语言指令 + 沙箱 | 默认让沙箱承担隔离，分类器只处理越界调用；「运行一切」明确标注为接受风险 |
| Codex | `approval_policy`（on-request / never / granular）+ `sandbox_mode` | 审批策略与沙箱边界是两个独立维度，组合出不同自治等级 |

三条共同结论：

1. **执行权在应用，不在模型。** Claude Code 的文档写得很直白：权限规则由 Claude Code 执行，不由模型执行。模型可以「请求」，但不能「决定」。
2. **预设降低决策成本，自定义保留精确控制。** 只给复选框会让普通用户面对七个陌生开关；只给预设又会让高级用户被最粗的档位绑住。
3. **审批必须可恢复，且失败关闭。** 用户不回应、关页面、网络断开，都不能等价于同意。

本项目采用「预设 + 自定义」的轻量组合：不引入分类器（没有额外的模型调用与误判面），也不做跨项目的规则文件（当前是单租户会话级产品）。真正执行判定的是服务端的一个 capability，而不是提示词。

## 权限模型

权限与风险等级**正交**，不能互相替代：

- `RiskLevel` 回答「这次调用有多不可逆」，用于 UI 提示与兜底拦截；
- `Permission` 回答「用户是否已经预先授权这一类能力」，用于减少重复审批。

七个权限键（`app/runtime/permissions.py`）：

| 键 | 名称 | 覆盖的能力 | 默认 |
|---|---|---|---|
| `read` | 读取数据 | 读文件、搜索结果等只读内容 | 授予 |
| `write` | 写入文件 | 创建、修改、删除工作区文件 | 逐次询问 |
| `execute` | 执行命令 | 运行 shell、脚本、安装依赖 | 逐次询问 |
| `network` | 联网访问 | 搜索引擎、网页抓取、外部网络接口 | 授予 |
| `external` | 外部工具 | MCP 第三方 server 的工具 | 逐次询问 |
| `manage` | 环境管理 | 重置/销毁隔离执行环境 | 逐次询问 |
| `dangerous` | 高风险操作 | 不可恢复的破坏性操作 | 逐次询问，且不在任何预设里 |

默认授权是 `read + network`，与加入权限功能前的行为完全一致：只读与联网自动执行，写入/执行/外部工具仍然每次询问。

### 预设

| 预设 | 包含 | 适用场景 |
|---|---|---|
| 每次询问（`ask`，默认） | `read` `network` | 审阅每一个有副作用的动作 |
| 标准（`standard`） | `read` `write` `network` | 让 Agent 改文件，但执行命令仍要确认 |
| 自动执行（`auto`） | `read` `write` `execute` `network` `external` | 长任务、减少打断 |
| 自定义（`custom`） | 任意组合 | 精确控制，或显式放开 `dangerous` |

`auto` 刻意不包含 `manage` 与 `dangerous`：即使选择「自动执行」，重置执行环境与高风险操作也应当逐次确认。要连它们一起放行，只能进入自定义并单独勾选，界面上这两项以红色呈现。

### 工具如何声明权限

`ManagedTool` 新增 `permissions` 元数据，登记在 `app/tools/__init__.py`：

| 工具 | 权限 |
|---|---|
| `tavily_search` | `network` |
| `sandbox_read_file` | `read` |
| `sandbox_write_file` | `write` |
| `sandbox_exec` | `execute` |
| `sandbox_reset` | `manage` |
| MCP 工具 | `external`（由 `RiskAnnotatingToolset` 统一补注解） |
| `ask_user_clarification` / `get_current_time` | 无 |

没有声明权限的写入类工具仍然会被风险等级拦下。这是有意的失败关闭：漏声明的后果是「多问一次」，而不是「静默放行」。

## 执行判定

闸门仍在 `app/runtime/approval.py` 的 `ApprovalGate.before_tool_execute`，判定顺序：

1. 工具声明的权限没有全部授予 → 拦；
2. 风险等级缺失或未知 → 拦；
3. `SAFE_WRITE` / `DANGEROUS`：如果工具声明了权限且已全部授予，视为用户预授权，放行；没有权限声明则仍拦；
4. 其余等级（`READ_ONLY` / `INTERACTIVE`）放行。

第 1 条对只读工具同样生效：用户取消「联网访问」后，搜索工具也应该先问一次，而不是因为风险等级是只读就绕过用户的明确选择。

权限集合通过 pydantic-ai 的 `deps` 传入，而不是 ContextVar：

```python
@dataclass(frozen=True)
class AgentRunDeps:
    permissions: frozenset[str] = field(default_factory=lambda: DEFAULT_PERMISSIONS)
```

`Agent` 用 `deps_type=AgentRunDeps` 装配，每轮 `run_stream_events(..., deps=AgentRunDeps(...))` 传入。同一个缓存 Agent 可以被不同会话、不同权限选择复用，装配期不固化任何人的授权。

## 一次完整的请求链路

```text
输入框选择权限
  → localStorage: bennett.permissions.v1.<session_id>
  → POST /api/chat { permissions: ["read","execute"], ... }
  → ChatRequest 校验权限键（未知键 422）
  → stream_chat_events 解析为 AgentRunDeps
  → ApprovalGate 按工具 metadata 判定
      ├─ 已授权 → 直接执行
      └─ 未授权 → 抛 ApprovalRequired
            → DeferredToolRequests → approval_required 事件
            → 客户端持久化待审批调用
            → 用户「批准一次」或「本次会话允许」
            → 续跑：approvals + 更新后的 permissions
```

### 事件契约

`approval_required.tool_calls[]` 新增两个字段：

```json
{
  "type": "approval_required",
  "tool_calls": [
    {
      "tool_call_id": "call_1",
      "tool_name": "sandbox_exec",
      "args": "{\"command\":\"ls\"}",
      "description": "在用户自己的执行环境里跑一条 shell 命令……",
      "risk_level": "safe_write",
      "permissions": ["execute"],
      "permission_labels": ["执行命令"]
    }
  ]
}
```

`permissions` 是**尚未授予**的权限（已授予的会被过滤掉）：如果拦截来自未知风险等级而非权限，
这个列表为空，前端不会展示「本次会话允许」按钮——授了也解决不了这次拦截。

前端「本次会话允许」按 `permissions` 写回 localStorage，并把同一组键放进续跑请求。服务端无状态，不保存「谁批准过什么」；续跑请求里的权限列表就是这一轮的完整授权快照。

缺失的审批决定仍然一律按拒绝处理。**关闭页面不等于同意。**

## 失败关闭的几种情形

| 情形 | 行为 |
|---|---|
| 客户端不传 `permissions`（老客户端 / 脚本） | 使用默认 `read + network` |
| 客户端传 `[]` | 明确不预授权任何能力，连只读工具也先问 |
| 权限键拼错 | 422，并列出可选权限；静默忽略会让用户以为已授权 |
| 工具没有权限声明但风险等级是写入 | 仍然逐次询问 |
| 工具没有风险等级 | 仍然拦，权限授予不能覆盖「未知」 |
| 审批决定缺失 | 按拒绝续跑，并明确告诉模型「是用户拒绝，不是工具坏了」 |

## 安全边界

- **权限不是身份。** 它只决定「工具能不能跳过审批」。沙箱租约、数据归属仍由服务端注入的 `user_id` 与 `session_id` 决定；模型编造不出作用域。
- **模型不能给自己授权。** 权限来自客户端请求体，模型只能发起工具调用，不能修改请求里的 `permissions`。
- **权限不是安全边界本身。** 真正的边界是工具自己的输入校验与沙箱隔离。审批是给人一次机会看，不是给不安全性打补丁。
- **本机存储不等于服务端状态。** 权限存在 localStorage，按会话隔离；换设备需要重新选择。这是有意的：授权是「此刻这个人」的决定，不应静默跟随账号漫游。
- **审计仍在工具轨迹里。** 已授权而自动执行的调用照常出现在 `tool_call` / `tool_result` 事件与消息记录中，用户事后可以回看。

## 已知限制与后续方向

- 当前只有 allow（授予）与 ask（未授予）两态，没有 deny 规则。需要「无论怎么授权都禁止」时，应加一层 deny 清单，并保证 deny 优先于任何预设。
- 权限按会话存储，没有跨设备同步。若产品需要「同一账号在所有设备沿用授权」，应把权限放进服务端会话表，而不是同步 localStorage。
- 没有按命令内容细分的规则（例如「允许 `npm test`，不允许 `rm`」）。`sandbox_exec` 是整类授权；更细的粒度需要解析命令，误判面会显著上升。
- `dangerous` 允许被自定义勾选。若面向非隔离环境，应改为硬编码「始终逐次询问」，不提供开关。

## 相关源码

- `app/runtime/permissions.py` — 权限目录、预设、校验与 `AgentRunDeps`
- `app/runtime/approval.py` — 风险等级 + 权限的执行闸门
- `app/runtime/tool_registry.py` — 工具 `permissions` 元数据
- `app/tools/__init__.py` — 内置工具的权限声明
- `app/tools/mcp_client.py` — MCP 工具的 `external` 注解
- `app/services/approval_flow.py` — 审批事件与权限字段
- `app/services/agent_factory.py` — `deps_type=AgentRunDeps`
- `app/services/chat_service.py` — 每轮解析权限并传入 Agent
- `frontend/src/lib/permissions.ts` — 预设推导、会话级 localStorage
- `frontend/src/components/PermissionSelector.tsx` — 输入框权限选择器
- `frontend/src/components/assistant-ui/elements/approval-card.tsx` — 审批卡与「本次会话允许」

## 参考

- [Claude Code: Choose a permission mode](https://code.claude.com/docs/en/permission-modes)
- [Claude Code: Configure permissions](https://code.claude.com/docs/en/permissions)
- [Cursor: Run Modes](https://cursor.com/docs/agent/security/run-modes)
- [ChatGPT/Codex: Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)
