/**
 * 失败分类的展示口径（前后端契约见 `app/schemas/events.py::StreamError`）。
 *
 * 服务端会给出三种机器可读的失败分类，但前端此前只认 `context_overflow`：另外两种
 * （`model_unavailable` / `internal_error`）在 `readErrorCode` 里被当成「未分类」丢掉，
 * 于是「模型已被管理员停用」和「服务端查库失败」在界面上长得一模一样——都只是一句
 * 通用报错。这个模块把「分类 → 该怎么称呼 / 该怎么提示」收敛到一处，纯函数可测。
 *
 * 认不出的分类一律当「未分类」：新版服务端可能加新值，旧前端不能把协议外的字符串
 * 带进 run（它会进落库、进 metadata，再被渲染层当判据）。
 */

import type { CompactionState } from '../types';
import { formatTokens } from './context-usage';

export type KnownErrorCode = 'context_overflow' | 'model_unavailable' | 'internal_error';

const KNOWN_CODES: readonly KnownErrorCode[] = [
  'context_overflow',
  'model_unavailable',
  'internal_error',
];

/** 收窄服务端事件里的 `code`：只认本版认识的取值，其余返回 null。 */
export function readKnownErrorCode(value: unknown): KnownErrorCode | null {
  return typeof value === 'string' && (KNOWN_CODES as readonly string[]).includes(value)
    ? (value as KnownErrorCode)
    : null;
}

/**
 * 消息正文里那条提示的标题。
 *
 * 「上下文已满」不是故障而是状态，措辞要与其它失败区分；「模型不可用」同理——
 * 它是配置变更的结果，重试同一个模型没有意义。
 */
export function describeErrorLabel(code: KnownErrorCode | null): string {
  switch (code) {
    case 'context_overflow':
      return '上下文已满';
    case 'model_unavailable':
      return '模型不可用';
    case 'internal_error':
      return '服务端错误';
    default:
      return '错误';
  }
}

/**
 * 失败下方那行小字给出的「下一步」。
 *
 * 只对能给出确定动作的分类返回文案；未分类的错误什么都不加——凭空建议一个动作
 * 比不给更糟（用户会照着做，然后发现没用）。
 */
export function describeErrorHint(code: KnownErrorCode | null): string {
  switch (code) {
    case 'model_unavailable':
      return '当前模型可能已被管理员停用或删除。请在输入框上方的模型菜单里换一个模型；菜单里的「重试」可以刷新可用列表。';
    case 'context_overflow':
      return '重试不会成功：历史只会更长。请新建会话，或换用上下文窗口更大的模型。';
    default:
      return '';
  }
}

/**
 * 上下文压缩提示行的文案（服务端事件见 `CompactionEvent`）。
 *
 * 与「已切换备用模型」同一原则：凡是改变了本轮回答依据的事都要说出来。这里额外
 * 说明**代价**——早期细节可能不再完整。不说，用户会把「模型忘了刚才说的」当成
 * 模型变笨，而现场没有任何痕迹能指向压缩。
 *
 * 数字用 `formatTokens`（与上下文用量指示器同一口径）：同一份数据在两处显示成
 * 「12k」与「12,000」会让人以为是两个不同的量。
 */
export function describeCompactionNotice(state: CompactionState): string {
  const saved = formatTokens(Math.max(state.usedTokensBefore - state.usedTokensAfter, 0));
  if (state.strategy === 'truncate') {
    return `上下文接近上限，已截断 ${state.truncatedMessages} 条超长消息的中间部分（省出约 ${saved} tokens）`;
  }
  const extra = state.truncatedMessages > 0 ? `，另有 ${state.truncatedMessages} 条超长消息被截断` : '';
  return `上下文接近上限，已把最早 ${state.droppedMessages} 条消息折叠成摘要${extra}（省出约 ${saved} tokens）；早期细节可能不再完整`;
}
