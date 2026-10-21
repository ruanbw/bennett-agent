/**
 * 模型选择器的状态推导：当前选中了哪一条、它还在不在列表里、哪一条是默认。
 *
 * 为什么需要单独一层：管理员在后台停用一个模型后，`/api/models` 会把它从列表里
 * 移除，但本机会话里仍保存着旧的路由 id。选择器如果只做 `models.find(...)`，就会
 * 退化成显示一个查不到名字的 id，用户也不知道「下一条消息为什么会被拒」。
 *
 * 三类状态分开返回而不是在组件里散落判断：
 * - `selected`：当前 id 对应的条目，查不到为 null；
 * - `selectedMissing`：列表**非空**且当前 id 查不到——这是「确定已不可用」。列表为空
 *   表示还没加载/加载失败，不能据此断言模型被停用；
 * - `defaultModel`：后端明确标了 `is_default` 的那一条；没有标记就返回 null，不拿
 *   列表第一条来猜。
 */

import type { ModelItem } from '../types';

export interface ModelSelectionState {
  selected: ModelItem | null;
  selectedMissing: boolean;
  defaultModel: ModelItem | null;
}

export function readModelSelection(
  models: readonly ModelItem[],
  currentModel: string | null | undefined,
): ModelSelectionState {
  const selected = currentModel ? (models.find((model) => model.id === currentModel) ?? null) : null;

  return {
    selected,
    selectedMissing: Boolean(currentModel) && models.length > 0 && selected === null,
    defaultModel: models.find((model) => model.is_default) ?? null,
  };
}

/**
 * 模型列表的首字母跳转。
 *
 * 同一提供商下经常有多条同名路由（`DeepSeek V4.1 Flash` × 4），键盘用户按 ↓ 一路
 * 数过去很累。这里按「展示名或第二行标签」的首字母跳转，从当前位置之后找、找不到再
 * 从列表头开始（与原生 `<select>` 的行为一致）。
 *
 * 只接受单个可见字符：方向键、Tab、Enter 由调用方先处理掉；输入法组合中的按键由调用方
 * 过滤。没有命中时返回 -1，调用方保持当前位置不变。
 */
export function findModelByPrefix(
  models: readonly { name: string; provider?: string | null; model_id?: string | null }[],
  char: string,
  fromIndex: number,
): number {
  if (models.length === 0) return -1;
  const needle = char.trim().toLowerCase();
  if (needle.length !== 1) return -1;

  const matches = (index: number): boolean => {
    const model = models[index];
    const haystacks = [model.name, model.provider ?? '', model.model_id ?? ''];
    return haystacks.some((value) => value.trim().toLowerCase().startsWith(needle));
  };

  const start = Math.max(0, fromIndex);
  for (let offset = 1; offset <= models.length; offset += 1) {
    const index = (start + offset) % models.length;
    if (matches(index)) return index;
  }
  return -1;
}
