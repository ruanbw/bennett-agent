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
