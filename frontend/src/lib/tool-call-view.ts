/**
 * 工具调用渲染的纯逻辑：状态判定、文本格式化与折叠阈值。
 *
 * 放在 lib 而不是组件文件里有两个原因：组件文件在 vitest 的 node 环境里跑不了
 * （没有 jsdom），而「这次调用到底算不算执行完成」正是最容易出错、最需要回归
 * 测试锁住的一条规则；顺带也让 tool-fallback.aui.tsx 只导出组件。
 */

import type { ToolCallMessagePartStatus } from '@assistant-ui/react';

/** 超过这个长度先只渲染前一段，是否铺开交给「展开全部」按钮。 */
export const TOOL_TEXT_PREVIEW_LIMIT = 2000;

/**
 * 工具调用在界面上的状态。
 *
 * 运行时只给四种；`interrupted` 是本层补的第五种：工具调用停在半途（点停止、
 * 断流、丢帧）时 result 永远缺席，但消息级 status 仍可能被写成 complete，
 * 直接沿用就会画出绿色对勾，让用户误信工具已经执行成功。
 */
export type ToolCallViewStatus =
  | 'running'
  | 'complete'
  | 'incomplete'
  | 'requires-action'
  | 'interrupted';

/**
 * 判定展示状态：只有拿到 result 才允许显示完成。
 *
 * 运行中的调用（running）与等待用户动作的调用（requires-action）不受影响——
 * 它们本来就不显示为完成。
 */
export function resolveToolCallViewStatus(
  status: ToolCallMessagePartStatus | undefined,
  result: unknown,
): ToolCallViewStatus {
  if (status?.type === 'running') return 'running';
  if (status?.type === 'incomplete') return 'incomplete';
  if (status?.type === 'requires-action') return 'requires-action';
  return result === undefined ? 'interrupted' : 'complete';
}

/** 工具结果可能是任意运行时值：先 JSON 化，失败再退化成字符串，最后兜底占位。 */
export const formatUnknownValue = (value: unknown, space?: number): string => {
  if (typeof value === 'string') return value;

  try {
    if (value instanceof Error) return String(value);

    const json = JSON.stringify(value, null, space);
    if (json !== undefined) return json;
  } catch {}

  try {
    return String(value);
  } catch {
    return '[Unserializable value]';
  }
};

/** 超长文本的预览切片，截断时补一个省略号，让用户看得出这里被截过。 */
export function previewToolText(
  text: string,
  limit = TOOL_TEXT_PREVIEW_LIMIT,
): { preview: string; truncated: boolean } {
  if (text.length <= limit) return { preview: text, truncated: false };
  return { preview: `${text.slice(0, limit)}…`, truncated: true };
}

/** 工具耗时文案：与 ToolFallbackDuration 共用，抽出来便于单测。 */
export const formatToolDuration = (ms: number): string => {
  if (ms < 1000) return '<1s';
  const seconds = ms / 1000;
  if (seconds < 10) return `${(Math.floor(seconds * 10) / 10).toFixed(1)}s`;
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
};
