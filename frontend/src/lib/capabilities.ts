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
 * 丢掉名字不是字符串的条目：`name` 也是工具名前缀，留一个 `undefined` 没有意义，
 * 还会让调用方在拼接工具名时出问题。
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
