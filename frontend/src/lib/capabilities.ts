/**
 * 运行时能力探针的客户端。
 *
 * 唯一用途：让空对话页的起步建议只承诺真正可用的能力。它是**装饰性数据**，
 * 失败时静默降级为「没有联网搜索」——不能让它阻塞登录、模型列表或对话本身。
 *
 * 响应形状见 `types.ts:Capabilities` 与 `app/schemas/capabilities.py`。
 */

import type { Capabilities, McpServerStatus } from '../types';
import { apiFetch } from './api';

/** 还没拿到（或拿不到）能力清单时的保守取值：不推荐任何依赖外部服务的能力。 */
export const UNKNOWN_CAPABILITIES: Capabilities = {
  web_search: false,
  sandbox: false,
  tools: [],
  mcp_servers: [],
  mcp_error: null,
};

/**
 * 能力探测的超时。
 *
 * 它是装饰性数据，但没有超时就会一直挂着：接口被反代攒住时，用户永远看不到
 * 「联网搜索可用」的示例，也没有任何提示说明为什么。超时后按「拿不到」处理。
 */
export const CAPABILITIES_TIMEOUT_MS = 8_000;

/**
 * 拉取能力清单，并把响应收窄成前端可用的形状。
 *
 * 不直接 `as Capabilities`：服务端将来增删字段、代理返回错误页时，前端不该因为一个
 * 类型断言拿到 `undefined` 再在筛选逻辑里炸掉。布尔值只认严格的 `true`——老服务端没有
 * `sandbox` 时因此自然退化成 false（不推荐沙箱相关的起步建议）。
 */
export async function fetchCapabilities(): Promise<Capabilities> {
  const res = await apiFetch('/api/capabilities', { timeoutMs: CAPABILITIES_TIMEOUT_MS });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const data: unknown = await res.json();
  const record = (data ?? {}) as Partial<Capabilities>;
  return {
    web_search: record.web_search === true,
    sandbox: record.sandbox === true,
    tools: Array.isArray(record.tools)
      ? record.tools.filter((name): name is string => typeof name === 'string')
      : [],
    mcp_servers: sanitizeMcpServers(record.mcp_servers),
    mcp_error: typeof record.mcp_error === 'string' && record.mcp_error ? record.mcp_error : null,
  };
}

/**
 * 收窄 MCP server 列表。
 *
 * 丢掉名字不是字符串的条目：`name` 是渲染键，也是工具名前缀。留一个 `undefined`
 * 进来会让 React 的 key 出问题，而且渲染出一个「无名 server」对排查毫无帮助。
 */
function sanitizeMcpServers(value: unknown): McpServerStatus[] {
  if (!Array.isArray(value)) return [];
  const servers: McpServerStatus[] = [];
  for (const entry of value) {
    if (!entry || typeof entry !== 'object') continue;
    const { name, tools } = entry as Partial<McpServerStatus>;
    if (typeof name !== 'string' || !name) continue;
    servers.push({
      name,
      tools: Array.isArray(tools)
        ? tools.filter((tool): tool is string => typeof tool === 'string')
        : [],
    });
  }
  return servers;
}

/**
 * 设置页里「服务端能力」一行的文案。
 *
 * 用户看到的示例是**裁剪过**的：没有联网搜索时搜索示例整条消失。这个裁剪如果没有
 * 任何说明，用户只会以为「这个助手就是不会搜索」，而实际上可能只是服务端没配 Key。
 * 把状态摆到设置里，问题就从「功能缺失」变成「部署没配」。
 */
export function describeWebSearchCapability(capabilities: Capabilities | null | undefined): string {
  if (!capabilities) return '未知';
  return capabilities.web_search ? '已启用' : '未启用';
}

/** 工具数量的说明：能力清单为空时不要显示「0 个工具」这种吓人的结论。 */
export function describeToolCount(capabilities: Capabilities | null | undefined): string {
  const count = capabilities?.tools.length ?? 0;
  return count > 0 ? `${count} 个` : '未知';
}

/**
 * MCP 的展示文案。
 *
 * 「配了但不通」必须能看出来：只让工具从列表里消失，用户会以为这个助手本来就没有
 * 那些能力，而运维会以为配置从来没生效。两句话分别对应「通了几个」与「为什么没通」。
 */
export function describeMcpCapability(capabilities: Capabilities | null | undefined): string {
  if (!capabilities) return '未知';
  const servers = capabilities.mcp_servers ?? [];
  if (servers.length === 0) {
    // 有错误但零个可用 = 配了全都不通；没错误 = 根本没配
    return capabilities.mcp_error ? '配置了但未能连接' : '未配置';
  }
  const toolCount = servers.reduce((sum, server) => sum + server.tools.length, 0);
  return `${servers.length} 个 server · ${toolCount} 个工具`;
}

/** MCP server 名字清单（设置页里展开显示；空串表示没有可列的东西）。 */
export function describeMcpServers(capabilities: Capabilities | null | undefined): string {
  const servers = capabilities?.mcp_servers ?? [];
  return servers.map((server) => server.name).join('、');
}

/**
 * MCP server 的明细行（设置页「运行时能力」分区用）。
 *
 * 与 `describeMcpServers`（只有名字、给悬浮提示用）不同：这里要能看出**每个 server
 * 各带了哪些工具**，否则「2 个 server · 3 个工具」这行字仍然解释不了「我要的那个工具
 * 到底在不在」。
 *
 * 返回数组而不是拼好的字符串：调用方要用 `name` 做 React key，拼成一个大字符串
 * 就只剩一层 `<p>`，读屏也念不出一对一的关系。
 */
export interface McpServerSummary {
  name: string;
  /** 该 server 的工具数。 */
  toolCount: number;
  /** 工具名清单（用于展开显示）。 */
  tools: string[];
}

export function summarizeMcpServers(
  capabilities: Capabilities | null | undefined,
): McpServerSummary[] {
  return (capabilities?.mcp_servers ?? []).map((server) => ({
    name: server.name,
    toolCount: server.tools.length,
    tools: [...server.tools],
  }));
}

/**
 * 一个 server 的工具清单文案。
 *
 * 没有工具时说「未提供工具」而不是空串：一个名字列在那里、后面什么都没有，读起来像
 * 渲染坏了，也让人分不清「这个 server 就是没有工具」和「前端没拿到工具」。
 */
export function describeServerTools(tools: readonly string[]): string {
  return tools.length > 0 ? tools.join('、') : '未提供工具';
}

/**
 * 工具清单的整体文案。
 *
 * 与 `describeToolCount` 分开：那个只给数量（「3 个」），这里要**列出名字**。
 * 数量回答「有没有」，名字回答「有没有我需要的那个」——排障时后者才是问题。
 */
export function describeToolList(capabilities: Capabilities | null | undefined): string {
  const tools = capabilities?.tools ?? [];
  return tools.length > 0 ? tools.join('、') : '暂无已装配的工具';
}

/**
 * `mcp_error` 的展示文案。
 *
 * 原文照抄，不加任何前缀：这句话是给运维看的，它是上游（MCP 客户端 / server 启动器）
 * 给出的原始错误，改写会丢掉排查最关键的细节（连接串、退出码、包名）。
 * 保留 `null` 与空串的区分：前者是「没有错误」，后者是「有错误但原因没给」，
 * 后者也要能被说出来，否则「配了但不通」仍然是哑的。
 */
export function describeMcpError(capabilities: Capabilities | null | undefined): string {
  if (!capabilities) return '';
  const error = capabilities.mcp_error;
  if (error === undefined || error === null) return '';
  return error || '（服务端没有给出具体原因）';
}

/**
 * 沙箱能力文案。
 *
 * 与 `describeWebSearchCapability` 同一套语义（未知 / 已启用 / 未启用），单独一个函数
 * 而不是传参复用：这两个字段以后可能演化出不同的解释（沙箱还有「按用户配额关闭」这类
 * 中间态），共用会逼着以后加分支参数。
 */
export function describeSandboxCapability(capabilities: Capabilities | null | undefined): string {
  if (!capabilities) return '未知';
  return capabilities.sandbox ? '已启用' : '未启用';
}
