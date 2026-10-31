/**
 * 插入排队的呈现层：输入框上方那叠「待发消息」。
 *
 * 数据面（`composer.queue`）与动作面（`aui.composer.queueItem` 的 edit/remove/move）
 * 都来自 assistant-ui 的队列适配器，本组件只做三件事：
 *   - 把每条排队消息渲染成一行（预览文本 + 序号）；
 *   - 「编辑」：把文本放回输入框并从队列移除——发送门在输入框有内容且本轮结束后
 *     照常工作，用户改完再点发送等于一次普通发送；
 *   - 「删除」：从队列移除；「立即发送」：移到 steer 位，本轮结束后第一个发出
 *     （不打断正在生成的回复）。
 *
 * 组件在 node 环境的测试里通过纯函数 `queueBarVisibility` 做可见性判定，渲染结构
 * 由浏览器侧覆盖。
 */

import { BanIcon, PencilIcon, PlayIcon, XIcon } from 'lucide-react';
import type { FC } from 'react';
import { useCallback } from 'react';
import {
  AuiIf,
  ComposerPrimitive,
  QueueItemPrimitive,
  useAui,
  useAuiState,
} from '@assistant-ui/react';
import { queueItemPreview } from '@/lib/message-queue-view';

/**
 * 排队条本体。
 *
 * AuiIf 的 children 是单个元素：内层再包一个 fragment 承担布局。
 */
export const MessageQueueBar: FC = () => {
  // 整条队列的可见性：一个 selector 返回长度（数字），比较是稳的。
  const queueLength = useAuiState((s) => s.composer.queue.length);
  if (queueLength === 0) return null;

  return (
    <div
      role="region"
      aria-label="待发送的排队消息"
      className="mb-2 rounded-xl border border-border/70 bg-muted/30 px-3 py-2"
    >
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium text-muted-foreground">
          排队中 · {queueLength} 条，本轮结束后依次发送
        </span>
        <AuiIf condition={(s) => s.thread.isRunning}>
          <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground/70">
            <BanIcon className="size-3" aria-hidden="true" />
            生成中，暂不打断
          </span>
        </AuiIf>
      </div>
      <ol className="flex flex-col gap-1">
        <ComposerPrimitive.Queue>
          {({ queueItem }) => <QueuedItemRow key={queueItem.id} text={queueItem.prompt} />}
        </ComposerPrimitive.Queue>
      </ol>
    </div>
  );
};

/**
 * 一条排队消息。
 *
 * 「立即发送」在空闲时没有意义（普通发送就是立即），只在生成期间出现——
 * steer 的语义是「本轮结束后第一个发」，不打断正在写的回复。
 */
const QueuedItemRow: FC<{ text: string }> = ({ text }) => {
  const aui = useAui();
  const isRunning = useAuiState((s) => s.thread.isRunning);

  const handleEdit = useCallback(() => {
    // 编辑 = 放回输入框 + 从队列移除。输入框此刻必然可用（没有「编辑中」的互斥）。
    void aui.composer.setText(text);
    aui.queueItem.remove();
  }, [aui, text]);

  return (
    <li className="group flex items-start gap-2 rounded-lg border border-border/50 bg-background px-2.5 py-1.5">
      <span
        className="min-w-0 flex-1 cursor-default text-xs leading-relaxed text-foreground/90 break-all"
        title={text}
      >
        {queueItemPreview(text)}
      </span>
      <span className="flex shrink-0 items-center gap-0.5 opacity-60 transition group-hover:opacity-100 group-focus-within:opacity-100">
        <button
          type="button"
          onClick={handleEdit}
          className="rounded p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
          title="改一下再发：放回输入框"
          aria-label="编辑这条排队消息"
        >
          <PencilIcon className="size-3.5" aria-hidden="true" />
        </button>
        {isRunning && (
          <QueueItemPrimitive.Steer asChild>
            <button
              type="button"
              className="rounded p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
              title="立即发送：本轮结束后第一个发出，不打断正在生成的回复"
              aria-label="立即发送这条排队消息"
            >
              <PlayIcon className="size-3.5" aria-hidden="true" />
            </button>
          </QueueItemPrimitive.Steer>
        )}
        <QueueItemPrimitive.Remove asChild>
          <button
            type="button"
            className="rounded p-1 text-muted-foreground transition hover:bg-destructive/10 hover:text-destructive focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
            title="删除这条排队消息"
            aria-label="删除这条排队消息"
          >
            <XIcon className="size-3.5" aria-hidden="true" />
          </button>
        </QueueItemPrimitive.Remove>
      </span>
    </li>
  );
};
