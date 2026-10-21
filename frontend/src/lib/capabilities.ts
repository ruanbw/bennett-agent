/**
 * 运行时能力探针的客户端。
 *
 * 唯一用途：让空对话页的起步建议只承诺真正可用的能力。它是**装饰性数据**，
 * 失败时静默降级为「没有联网搜索」——不能让它阻塞登录、模型列表或对话本身。
 *
 * 响应形状见 `types.ts:Capabilities` 与 `app/schemas/capabilities.py`。
 */

import type { Capabilities } from '../types';
import { apiFetch } from './api';

/** 还没拿到（或拿不到）能力清单时的保守取值：不推荐任何依赖外部服务的能力。 */
export const UNKNOWN_CAPABILITIES: Capabilities = { web_search: false, tools: [] };

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
 * 类型断言拿到 `undefined` 再在筛选逻辑里炸掉。布尔值只认严格的 `true`。
 */
export async function fetchCapabilities(): Promise<Capabilities> {
  const res = await apiFetch('/api/capabilities', { timeoutMs: CAPABILITIES_TIMEOUT_MS });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const data: unknown = await res.json();
  const record = (data ?? {}) as Partial<Capabilities>;
  return {
    web_search: record.web_search === true,
    tools: Array.isArray(record.tools)
      ? record.tools.filter((name): name is string => typeof name === 'string')
      : [],
  };
}

/**
 * 设置面板里「服务端能力」一行的文案。
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
