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

/**
 * 参数一览：折叠状态下也让人看懂「这次工具调用在干什么」。
 *
 * 优先取常见的主参数（`query` / `q` / `url` / `question` …）——模型生成的参数对象往往
 * 带一堆可选字段，直接展示 JSON 既长又全是噪音。取不到主参数时退回原始 argsText
 * （非 JSON 的纯文本也走这条路）。结果按字符（码点）截断，避免劈开 emoji。
 *
 * 返回值只用于**展示**：不参与任何判断，空字符串表示没有可展示的内容。
 */
const PREVIEW_ARG_KEYS = ['query', 'q', 'question', 'url', 'path', 'command', 'text', 'prompt', 'name', 'id'] as const;

export function toolArgsPreview(argsText: string | null | undefined, limit = 48): string {
  if (!argsText) return '';

  let value = argsText;
  try {
    const parsed: unknown = JSON.parse(argsText);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const record = parsed as Record<string, unknown>;
      // `{}` / 全空参数没有可展示的内容：显示一个 `{}` 只是噪音
      if (Object.keys(record).length === 0) return '';

      const preferred = PREVIEW_ARG_KEYS
        .map((key) => record[key])
        .find((candidate): candidate is string => typeof candidate === 'string' && candidate.trim().length > 0);
      if (preferred) {
        value = preferred;
      } else {
        // 没有约定字段时，取第一个非空字符串，仍比整段 JSON 好读
        const firstString = Object.values(record).find(
          (candidate): candidate is string => typeof candidate === 'string' && candidate.trim().length > 0,
        );
        if (firstString) value = firstString;
      }
    } else if (Array.isArray(parsed)) {
      const firstString = parsed.find(
        (candidate): candidate is string => typeof candidate === 'string' && candidate.trim().length > 0,
      );
      if (firstString === undefined) return '';
      value = firstString;
    } else if (typeof parsed === 'string') {
      value = parsed;
    }
  } catch {
    // 非 JSON：原样展示（流式过程中 argsText 可能还没拼成合法 JSON）
  }

  const flat = value.replace(/\s+/g, ' ').trim();
  if (!flat) return '';
  const chars = Array.from(flat);
  return chars.length <= limit ? flat : `${chars.slice(0, limit).join('')}…`;
}

/** 工具耗时文案：与 ToolFallbackDuration 共用，抽出来便于单测。 */
export const formatToolDuration = (ms: number): string => {
  if (ms < 1000) return '<1s';
  const seconds = ms / 1000;
  if (seconds < 10) return `${(Math.floor(seconds * 10) / 10).toFixed(1)}s`;
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
};
