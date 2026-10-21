/**
 * `/api/models` 响应的形状校验。
 *
 * 服务端返回的 JSON 是**外部输入**：网关升级、反代改写、或者后端换了字段名，都可能
 * 让 `models` 变成字符串或对象。此前 App 直接 `data.models ?? []` 就塞进 state，
 * 于是 `models.map` 在渲染时抛 `models.map is not a function`——整个应用被 ErrorBoundary
 * 兜住，用户看到一整屏「应用出现异常」，而真正的问题只是模型菜单坏了。
 *
 * 这里把「拿得到什么」和「用不用得了」分开：逐条校验，坏的条目丢掉并计数，整份响应
 * 认不出来就抛一个**可读**的错误（由调用方翻成中文）。不引入 zod：字段少、规则简单，
 * 手写校验更容易读，也不会为一次解析引入运行时依赖。
 */

import type { ModelItem, ModelListResponse } from '../types';

/** 校验结果：可用的模型、被丢掉的条目数、默认模型。 */
export interface ParsedModelList {
  models: ModelItem[];
  /** 形状不合法的条目数（用于告警，不阻塞其余条目）。 */
  dropped: number;
  defaultModel: string | null;
}

/** 响应体根本认不出来时抛这个：消息是给开发者看的，调用方负责翻成用户文案。 */
export class ModelListShapeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ModelListShapeError';
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

/**
 * 单条模型的最小可用性判定：`id` 与 `name` 是渲染必需的，其余字段缺失时按默认值补齐。
 *
 * 不因为 `reasoning` 缺失就丢条目：老网关不返回这个字段，但模型本身是可用的。
 */
function parseModelItem(value: unknown): ModelItem | null {
  const record = asRecord(value);
  if (!record) return null;

  const id = asString(record.id);
  if (!id) return null;

  const levels = Array.isArray(record.supported_reasoning_levels)
    ? record.supported_reasoning_levels.filter((level): level is string => typeof level === 'string')
    : [];

  return {
    id,
    name: asString(record.name) ?? id,
    provider: asString(record.provider) ?? '',
    model_id: asString(record.model_id),
    description: asString(record.description),
    // 三态：true / false / null（网关未声明）。其它取值一律当「未声明」。
    reasoning: typeof record.reasoning === 'boolean' ? record.reasoning : null,
    supported_reasoning_levels: levels,
    context_window: typeof record.context_window === 'number' ? record.context_window : null,
    max_output_tokens: typeof record.max_output_tokens === 'number' ? record.max_output_tokens : null,
    is_default: record.is_default === true,
  };
}

/**
 * 解析响应体。
 *
 * `models` 不是数组时直接抛错，而不是当成空列表：空列表会被显示成「模型列表为空」，
 * 让用户以为管理员没配模型，而真实原因是响应格式不对——两种情况的下一步动作不同。
 */
export function parseModelListResponse(payload: unknown): ParsedModelList {
  const record = asRecord(payload);
  if (!record) throw new ModelListShapeError('响应不是 JSON 对象');

  const rawModels = record.models;
  if (!Array.isArray(rawModels)) {
    throw new ModelListShapeError(`models 字段不是数组（收到 ${typeof rawModels}）`);
  }

  const models: ModelItem[] = [];
  let dropped = 0;
  for (const raw of rawModels) {
    const item = parseModelItem(raw);
    if (item) models.push(item);
    else dropped += 1;
  }

  return {
    models,
    dropped,
    defaultModel: asString(record.default_model),
  };
}

/** 便捷包装：从 `Response` 读到 `ParsedModelList`。 */
export async function readModelListResponse(response: Response): Promise<ParsedModelList> {
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ModelListShapeError('响应不是合法 JSON');
  }
  return parseModelListResponse(payload);
}

/** 类型层面确认与后端契约同形（编译期检查，运行时不做任何事）。 */
export type _ModelListResponseShape = ModelListResponse;
