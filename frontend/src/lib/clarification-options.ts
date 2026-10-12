/**
 * 意图确认选项的清洗规则（纯函数，node 环境可直接单测）。
 *
 * 选项是模型输出：`app/services/chat_service.py` 只过滤了非字符串，空串、重复项、
 * 上百条长文本都会原样到达弹层。这些都必须在这里收口——空串药丸被点中会让提问
 * 消失却什么都没提交，重复文本会让 React 报 key 冲突。
 */

/** 一次最多平铺展示多少个选项，其余收进「展开更多」。 */
export const MAX_VISIBLE_CLARIFICATION_OPTIONS = 8;

/**
 * 去首尾空白、丢弃空串、去重（保留首次出现顺序）。
 *
 * 不去重就没法用「选项文本 + 位置」当 key；不丢弃空串则会出现点了没反应的药丸。
 */
export function sanitizeClarificationOptions(
  options: readonly string[] | null | undefined,
): string[] {
  const seen = new Set<string>();
  const cleaned: string[] = [];

  for (const raw of options ?? []) {
    if (typeof raw !== 'string') continue;
    const option = raw.trim();
    if (!option || seen.has(option)) continue;
    seen.add(option);
    cleaned.push(option);
  }

  return cleaned;
}

/** 按「是否展开」切开清洗后的选项，返回要显示的与仍被折叠的数量。 */
export function visibleClarificationOptions(
  options: readonly string[],
  expanded: boolean,
): { visible: string[]; hiddenCount: number } {
  const visible = expanded ? [...options] : options.slice(0, MAX_VISIBLE_CLARIFICATION_OPTIONS);
  return { visible, hiddenCount: options.length - visible.length };
}

/**
 * 是否渲染自定义输入框。
 *
 * 清洗后一个有效选项都不剩时，输入框是用户唯一的出路；此时即便后端说
 * allow_custom_input=false 也要渲染，否则弹层只剩一个「忽略」按钮。
 */
export function shouldShowCustomInput(
  optionCount: number,
  allowCustomInput: boolean,
): boolean {
  return allowCustomInput || optionCount === 0;
}
