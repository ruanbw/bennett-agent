/**
 * 思考等级词表与归一化的唯一权威（R-7）。
 *
 * 改动前 `'off' | 'none' | 'false'` 的归一化散在三处、规则各异：
 *   - `app/services/chat_service.py`：`"none" if x in ("off","none","false") else x`
 *   - `ReasoningEffortSelector.jsx`：`currentEffort === 'off' || currentEffort === 'none'`
 *   - `App.jsx`：`nextEffort !== 'off' && nextEffort !== 'none' && ...`
 * 三处只要有一处漏改，界面显示的等级与实际发给模型的参数就会不一致，而且不会报错。
 *
 * 后端侧的对应实现是 `app/services/agent_factory.py:normalize_reasoning_effort`。
 */

import type { ModelItem } from '../types';

/** 表示「关闭思考」的各种写法。 */
const OFF_ALIASES = new Set(['off', 'none', 'false', 'disabled']);

/** 归一化后的关闭标记。`'none'` 是发给网关的取值，前端统一用 `'off'` 表示。 */
export const OFF = 'off';

/**
 * 网关只说「该模型支持思考」但没有枚举等级时使用的保守集合。
 *
 * 这不是凭空发明：只在已经知道模型支持思考（`reasoning !== false`）或模型信息
 * 未知时才用它。见 `levelsFor`。
 */
export const DEFAULT_LEVELS: readonly string[] = ['low', 'medium', 'high'];

/** 当前等级不被新模型支持时的首选回退值。 */
export const PREFERRED_LEVEL = 'medium';

/** 是否为「关闭思考」。 */
export function isOff(effort: unknown): boolean {
  if (typeof effort !== 'string') return false;
  return OFF_ALIASES.has(effort.trim().toLowerCase());
}

/**
 * 归一化成前端内部规范值：`'off'` 或小写等级名。
 *
 * 返回 `null` 表示没有有效取值（空串、非字符串）。
 */
export function normalizeEffort(effort: unknown): string | null {
  if (typeof effort !== 'string') return null;
  const trimmed = effort.trim().toLowerCase();
  if (!trimmed) return null;
  return isOff(trimmed) ? OFF : trimmed;
}

function normalizeLevelList(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];

  const seen = new Set<string>();
  for (const item of raw) {
    const level = normalizeEffort(item);
    if (level && level !== OFF) seen.add(level);
  }
  return [...seen];
}

/**
 * 该模型可选的思考等级（含 `'off'`）。
 *
 * 三条规则，顺序重要：
 * 1. 模型明确声明「不支持思考」（`reasoning === false`）→ 返回空数组。
 *    `app/services/model_service.py` 的兜底模型正是 `reasoning: false` + 空等级，
 *    它的注释写明这是为了「避免用户选择后请求失败」——这里必须尊重它。
 * 2. 模型支持思考且枚举了等级 → 用后端给的，加上 `'off'`。
 * 3. 模型支持思考但没枚举等级（很多 OpenAI 兼容网关不返回该字段），或模型信息
 *    尚不可用 → 用 `DEFAULT_LEVELS`。
 */
export function levelsFor(model?: ModelItem | null): string[] {
  if (model && model.reasoning === false) return [];

  const declared = normalizeLevelList(model?.supported_reasoning_levels);
  const levels = declared.length > 0 ? declared : [...DEFAULT_LEVELS];
  return [OFF, ...levels];
}

/** 是否应展示思考等级选择器。模型明确不支持思考时不展示。 */
export function shouldShowEffortSelector(model?: ModelItem | null): boolean {
  return !model || model.reasoning !== false;
}

/**
 * 把当前等级对齐到目标模型支持的集合。
 *
 * 绝不返回模型未声明的等级——那正是「用户选了却请求失败」的来源。
 * 当前等级已合法时原样保留（包括 `'off'`：用户主动关掉思考的意图不该被模型切换抹掉）。
 */
export function reconcileEffort(currentEffort: unknown, model?: ModelItem | null): string {
  const normalized = normalizeEffort(currentEffort);
  const levels = levelsFor(model);

  // 模型不支持思考：不要请求任何等级
  if (levels.length === 0) return OFF;

  if (normalized && levels.includes(normalized)) return normalized;

  return levels.includes(PREFERRED_LEVEL) ? PREFERRED_LEVEL : (levels.find((l) => l !== OFF) ?? OFF);
}
