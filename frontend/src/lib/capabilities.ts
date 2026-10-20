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
 * 拉取能力清单，并把响应收窄成前端可用的形状。
 *
 * 不直接 `as Capabilities`：服务端将来增删字段、代理返回错误页时，前端不该因为一个
 * 类型断言拿到 `undefined` 再在筛选逻辑里炸掉。布尔值只认严格的 `true`。
 */
export async function fetchCapabilities(): Promise<Capabilities> {
  const res = await apiFetch('/api/capabilities');
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
