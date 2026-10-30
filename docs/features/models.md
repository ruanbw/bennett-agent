---
title: 模型接入与容灾
description: 提供商与模型的两层数据模型、OpenAI 兼容端点接入、密钥加密、SSRF 防护与备用模型切换矩阵。
---

模型接入只有一条路径：所有厂商都走 OpenAI 兼容端点，对外统一成 LiteLLM 路由键（`openai/gpt-4o` 形状）。厂商差异（Base URL、鉴权头、固定请求头）落在「提供商」这一层，模型差异（上下文窗口、输出上限、思考等级）落在「模型」这一层。两层数据都在数据库里，管理后台是唯一权威，改完立即生效，不需要重启进程。

## 两层数据模型

| 层 | 表 | 关键列 | 谁在用 |
|---|---|---|---|
| 提供商 | `model_provider` | `slug`、`base_url`、`api_key`、`auth_style`、`extra_headers`、`enabled` | 一次上游寻址的全部信息 |
| 模型 | `provider_model` | `provider_id`、`model_id`、`display_name`、`context_window`、`max_output_tokens`、`reasoning`、`supported_reasoning_levels`、`enabled`、`is_default` | 一次调用的参数与可见性 |

对外路由键是 `{provider.slug}/{model_id}`，例如 `deepseek/deepseek-chat`。`slug` 只在创建时由名称生成一次（`provider_repo.slugify_provider_name`），之后改名不改 `slug`——否则用户已经保存的模型选择会被悄悄改掉。

生成规则：名称转小写、非字母数字折成 `-`、截 48 字符；名称里没有任何 ASCII 字母数字时（中文名很常见，例如「默认网关」）退回 `p<8 位随机>`；与已有 `slug` 撞车时追加 `-2`、`-3`。所以中文名提供商的对外键长得像 `p1a2b3c4/deepseek-chat`，而不是名称的拼音。

`model_id` 是发给上游的名字，不参与 URL；不同提供商的同名模型靠 `slug` 前缀区分，不会互相覆盖。`(provider_id, model_id)` 有唯一约束，重复同步是 upsert 而不是插重复行。

## 预设与同步

内置预设（`app/services/model_service/presets.py`）是静态目录，覆盖 OpenAI、Anthropic、Google Gemini、DeepSeek、Kimi、阿里云百炼、智谱、OpenRouter、Groq、Mistral、Together、Fireworks、xAI、SiliconFlow、火山方舟，以及本机的 Ollama 与 LM Studio。

预设只在**创建时**把 `base_url` / `auth_style` / `extra_headers` 抄进库。运行时只读库，所以预设目录更新不会改动已有提供商——要跟进只能删掉重建。认证方式由 `auth_style` 决定 key 放在哪个头：`bearer` / `x-api-key` / `x-goog-api-key` / `none`。

「同步模型」是显式动作：把 `base_url` 拼成模型列表地址请求一次。路径里已有 `v<数字>` 段（`/v1`、`/v1beta/openai`、`/api/paas/v4`）就直接拼 `/models`，否则补 `/v1/models`——重复拼 `/v1` 会让提供商返回 404，而 404 很容易被误读成「提供商不可用」。兼容裸数组、`data` 数组、`models` 数组三种形状，只识别 Anthropic 形状的分页（`has_more` + `last_id`），翻页上限 20 页、每页超时 15 秒。同步是**只增不删**：上游这次没返回的模型不会被删（可能只是一次返回不完整），已存在模型的 `enabled` 与 `is_default` 也保持不变；只有 `context_window` / `max_output_tokens` 在上游给了非空值时才覆盖——大多数厂商的 `/v1/models` 不报窗口，无条件赋值会把 NULL 写进手填值，而 NULL 等于「不知道窗口」，上下文守卫会被永久静默关闭。

首次启动时可以用 `LITELLM_API_BASE` / `MODEL_NAME` 引导出一条名为「默认网关」的提供商（`BOOTSTRAP_PROVIDER_FROM_ENV`，默认开启；只写库、不联网）。引导前会校验地址格式，失败只告警不阻止启动。

引导只发生在「全新库」上：一旦写过初始化标记（`app_setting` 的 `model_registry_initialized`，首次引导或首次在后台创建提供商时写入），标记永不清除，所以**删光提供商后重启不会再次引导**。

只有「库里一条提供商都没有、也没有这个标记」的库才会退回旧版环境变量网关（`model_service/gateway.py`）：列表来自 `LITELLM_API_BASE` 的模型接口并缓存 5 分钟；上游拉不到时兜底成只报 `MODEL_NAME` 一项（负缓存 10 秒），可用模型白名单同时收敛到 `MODEL_NAME` 与（若配了）`FALLBACK_MODEL_NAME`——否则网关抖一下，任意登录用户就能用服务端 key 调未启用的高价模型。

## 启用与默认

- 提供商 `enabled=false`：该提供商下所有模型从用户可见清单里消失。
- 模型 `enabled=false`：该模型不可选；正在进行的对话不受影响。停用模型会连带取消它的默认标记。
- `is_default`：全局只能有一个。设默认会清掉其他模型的默认标记；**停用的模型不能设为默认**（服务层直接拒绝，400）。

默认模型的解析顺序：先看显式 `is_default`，没有则取展示顺序第一条。展示顺序与后台同源：提供商按 `created_at`，模型按 `sort_order`、`display_name`。

库里接管后一个启用模型都没有时，对话明确拒绝（503），不会退回环境变量。

## 密钥的加密与审计

`model_provider.api_key` 用 `EncryptedText` 列落库：`AUTH_SECRET` 经 SHA-256 派生 Fernet 密钥，密文以 `enc:v1:` 前缀存储，读取时透明解密。历史明文行原样返回（不写迁移脚本），下次保存时自然加密。

对外只返回掩码 `api_key_hint`（`mask_api_key`）：前 3 位 + `…` + 后 4 位；长度 ≤ 8 的 key 直接打 `••••`，因为短 key 露两个字符就等于泄露一半。换掉 `AUTH_SECRET` 会让旧密文解不开：此时 `EncryptedText` 返回 `None`，表现是「上游 401 + 后台显示未设置密钥」。启动时 `audit_provider_key_decryption()` 逐条试解并点名，`/health/ready` 里也能看到具体是哪几个提供商。

**换 Base URL 必须重新填写 API Key**：新地址的主人能在下次「测试连接」时收到库里那份明文 key，所以服务层在有 key 时拦下只改地址的保存。

## 出站 SSRF 防护

出站守卫在所有出站点生效：建/改提供商、拉模型列表、对话出站（`_build_target`）三处都要过。

- 链路本地、未指定、组播地址，以及云厂商元数据地址（`169.254.169.254`、`100.100.100.200`、`fd00:ec2::254`）**任何配置下都拒绝**。
- 环回 / 私有 / 保留段默认放行（本机 LiteLLM 网关与自建 vLLM 常在内网）。`PROVIDER_BLOCK_PRIVATE_TARGETS=true` 时才拒绝。
- IPv6 会拆出内嵌 IPv4（v4-mapped、6to4、teredo、NAT64、v4-compatible）一起判定，否则 `64:ff9b::a9fe:a9fe` 这种 `is_global` 为真的地址等于放行了 `169.254.169.254`。
- 主机名先解析再判定，十进制写法（`http://2130706433/`）同样会被拦。
- 判定结果**不缓存**，每次出站重新解析，否则 DNS rebinding 的窗口会从单次请求拉长到整个进程生命周期。
- 对话链路的 httpx 客户端 `follow_redirects=False`：守卫只校验管理员填的首跳，307 会把请求送到 `Location` 指向的内网地址。

`http://` 且主机解析为公网地址、且已配 API Key 时会打一条告警（API Key 将以明文经过公网），只告警不拒绝；只要有一个解析结果是内网就不下结论——管理员可能在 VPN 后面。

## 模型快照

一轮对话解析出的模型信息是一次性快照（`ChatModelSnapshot`）：路由键、调用目标、`context_window`、`max_output_tokens`、备用模型键与它的窗口、可用模型集合、思考等级集合。

`context_window` 与备用模型的窗口分开存：备用模型接手时，用量事件必须报真正回答的那个模型的窗口，而不是主模型的。窗口为 `None` 的语义是「无法判定」，上下文守卫据此放行而不是猜一个默认值。

## 备用模型切换矩阵

备用模型由 `FALLBACK_MODEL_NAME` 指定，**只在同一个提供商内找**：跨提供商的参数兼容性无法保证（例如思考等级词表不同），因此不做跨提供商兜底。名字为空、或与主模型同名（`configured_fallback_name`）都视为不启用——同名只会对同一个模型再打一次，还把错误变成更绕的异常组。配了却在库里找不到对应的启用模型时，会打一条 WARNING 说明原因——静默不兜底时用户只会直接看到失败。

| 上游失败 | 同模型重试 | 切备用模型 |
|---|---|---|
| 5xx | 是 | 是 |
| 429（限流） | 否（再打一次只会加深限流） | 是（限流可能只作用于当前模型） |
| 超时 | 是 | 是 |
| 连接失败（transport error） | 是 | 是 |
| 401 / 403 | 否 | 否（同一套凭据下备用也会失败） |
| 400 / 404 | 否 | 否（问题在请求本身） |

两条硬约束：

1. **只在还没输出任何内容时切换**。流已经打开并吐过内容后，中途失败不再接管——那样会把两段回答拼在一起。切换只发生在流打开阶段，之后由对话服务做整轮重试。
2. **备用模型必须能接受主模型的 `model_settings`**（思考等级、上游超时等），否则切换本身会 400。这是「只在同一提供商内找」的直接原因。

判定由 PydanticAI `FallbackModel` 的 `fallback_on` 回调完成，口径来自 `upstream_errors.is_fallback_eligible`。

切换会向下发一个 `fallback` 事件（`from_model` / `to_model` / `reason`），原因短语由 `describe_fallback_reason` 生成，只有五个取值：「上游限流」、「上游 503」（带具体 5xx 码）、「请求超时」、「连接失败」、「上游错误」。前端据此显示「已切换备用模型」，同时计入 Prometheus 指标 `model_fallbacks_total`（标签 `from` / `to`）。

## 思考等级的归一化与校验

`reasoning_effort` 先归一化再进缓存：去空白、转小写；`off` / `none` / `false` / `disabled` 统一收敛成 `none`（各网关对「关闭思考」的写法不统一，不收敛会出现「界面显示已关闭、实际还开着」）。空值表示不向上游传该参数，用网关默认值。

`provider_model.reasoning` 只是「这个模型支持思考」的展示位，服务端不据它做任何判定；校验与传参都只看 `supported_reasoning_levels`。

校验发生在对话入口：请求的等级必须落在该模型 `supported_reasoning_levels` 声明的集合内，否则 400 并列出可选等级。模型没声明等级时不做校验（网关可以自定义等级，枚举会误杀）。`none` 始终允许——多数网关把它当独立开关而不是等级项。

## 相关源码

- `app/services/model_service/registry.py` — 模型解析、默认模型、对外清单
- `app/services/model_service/provider_sync.py` — 写用例、上游同步、环境变量引导
- `app/services/model_service/outbound_guard.py` — 出站地址守卫
- `app/services/model_service/presets.py` — 内置厂商预设目录
- `app/services/upstream_errors.py` — 错误分类与切换判定口径
- `app/services/agent_factory.py` — Agent 装配、备用模型包装、思考等级归一化
- `app/config/secret_box.py` — API Key 落库加密
