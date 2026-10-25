import { useState } from 'react';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  /**
   * 外部原因暂时不能确认（例如还在核对「有没有未同步内容」）。
   *
   * 与内部的 `pending` 分开：`pending` 是「已经按过确认」，这里只是「还不能按」。
   */
  confirmDisabled?: boolean;
  onConfirm: () => void;
}

/**
 * 确认对话框。
 *
 * 取代 `window.confirm()`：后者阻塞主线程、样式完全不受应用控制、在部分嵌入环境
 * （iframe / PWA / 某些自动化环境）里被禁用，而且无法测试。项目里本来就有 Dialog
 * 基元，附件预览已经在用。
 *
 * 组件只负责呈现与回传决定；「确认后做什么」由调用方以描述性状态持有。
 *
 * 两个破坏性场景的必需语义：
 * - `role="alertdialog"`：读屏会立刻朗读标题与描述，而不是当普通对话框处理。
 *   `aria-labelledby` / `aria-describedby` 由 base-ui 从 DialogTitle / DialogDescription
 *   自动接上，不需要手写 id。
 * - 一次性 guard：弹层在关闭动画期间仍在 DOM 里，双击（或按两下回车）会重复触发
 *   onConfirm——那是「删除两次」级别的 bug。
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel = '确认',
  cancelLabel = '取消',
  destructive = false,
  confirmDisabled = false,
  onConfirm,
}: ConfirmDialogProps) {
  const [pending, setPending] = useState(false);
  const [wasOpen, setWasOpen] = useState(open);

  // prop 变化时在渲染期调整 state（React 官方 pattern），避免 effect + setState 多跑一轮渲染，
  // 也是 react(set-state-in-effect) 想拦的写法。重置只发生在「重新打开」这一刻：若在关闭时
  // 重置，关闭动画期间（弹层还在 DOM 上）的第二次点击就又被放行了。
  if (wasOpen !== open) {
    setWasOpen(open);
    if (open) setPending(false);
  }

  const handleConfirm = () => {
    // 一次性 guard：确认到下次打开之间不再响应（disabled 属性已挡掉真实双击，
    // 这里再挡一层键盘/程序化触发）
    if (pending || confirmDisabled) return;
    setPending(true);
    onOpenChange(false);
    onConfirm();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* base-ui 的 Dialog 默认 role="dialog"，破坏性确认必须显式改成 alertdialog */}
      <DialogContent role="alertdialog">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={pending}>
            {cancelLabel}
          </Button>
          <Button
            variant={destructive ? 'destructive' : 'default'}
            onClick={handleConfirm}
            disabled={pending || confirmDisabled}
          >
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
