import { Pin, PinOff, Trash2 } from 'lucide-react';

export interface SidebarBatchBarProps {
  selectedCount: number;
  isAllSelected: boolean;
  onToggleSelectAll: () => void;
  onExitSelectMode: () => void;
  // 批量操作允许 async 实现；面向用户的失败提示由调用方（Sidebar）负责
  onPinSelected: () => void | Promise<unknown>;
  onUnpinSelected: () => void | Promise<unknown>;
  onRequestBatchDelete: () => void | Promise<unknown>;
  onRequestClearAll: () => void | Promise<unknown>;
  canClearAll: boolean;
}

/** 触发可能返回 Promise 的回调并接住 rejection：不产生 unhandled rejection。 */
function trigger(action: () => void | Promise<unknown>, label: string): void {
  void Promise.resolve(action()).catch((err: unknown) => console.error(`${label}:`, err));
}

/**
 * 多选模式下的批量操作栏。
 *
 * 从 Sidebar 拆出：原文件同时承担会话 CRUD、搜索、多选、批量操作与主题切换。
 * `aria-live` 让「已选 N 项」的变化对读屏可见——此前它只是视觉文本。
 */
export function SidebarBatchBar({
  selectedCount,
  isAllSelected,
  onToggleSelectAll,
  onExitSelectMode,
  onPinSelected,
  onUnpinSelected,
  onRequestBatchDelete,
  onRequestClearAll,
  canClearAll,
}: SidebarBatchBarProps) {
  const isEmpty = selectedCount === 0;

  return (
    <div className="px-3 py-1.5 bg-muted/60 text-xs flex flex-col gap-1.5">
      <div className="flex items-center justify-between text-muted-foreground">
        <span className="font-medium text-foreground" aria-live="polite">
          已选 {selectedCount} 项
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onToggleSelectAll}
            className="hover:text-foreground cursor-pointer underline text-[11px]"
          >
            {isAllSelected ? '取消全选' : '全选'}
          </button>
          <button
            type="button"
            onClick={onExitSelectMode}
            className="hover:text-foreground cursor-pointer text-[11px]"
          >
            完成
          </button>
        </div>
      </div>

      <div className="flex items-center gap-1.5 pt-1">
        <button
          type="button"
          onClick={() => trigger(onPinSelected, '置顶选中会话失败')}
          disabled={isEmpty}
          className="flex-1 flex items-center justify-center gap-1 py-1 px-1.5 rounded bg-background hover:bg-accent text-[11px] disabled:opacity-50 cursor-pointer"
          title="置顶选中会话"
        >
          <Pin size={11} aria-hidden="true" />
          <span>置顶</span>
        </button>
        <button
          type="button"
          onClick={() => trigger(onUnpinSelected, '取消置顶失败')}
          disabled={isEmpty}
          className="flex-1 flex items-center justify-center gap-1 py-1 px-1.5 rounded bg-background hover:bg-accent text-[11px] disabled:opacity-50 cursor-pointer"
          title="取消选中会话的置顶"
        >
          <PinOff size={11} aria-hidden="true" />
          <span>取消置顶</span>
        </button>
        <button
          type="button"
          onClick={() => trigger(onRequestBatchDelete, '批量删除失败')}
          disabled={isEmpty}
          className="flex-1 flex items-center justify-center gap-1 py-1 px-1.5 rounded bg-destructive/10 text-destructive hover:bg-destructive/20 text-[11px] disabled:opacity-50 cursor-pointer"
          title="删除选中"
        >
          <Trash2 size={11} aria-hidden="true" />
          <span>删除</span>
        </button>
      </div>

      {canClearAll && (
        <button
          type="button"
          onClick={() => trigger(onRequestClearAll, '清空会话失败')}
          className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 mt-0.5 text-[11px] text-destructive hover:bg-destructive/10 rounded-md transition cursor-pointer"
          title="清空所有会话记录"
        >
          <Trash2 size={12} aria-hidden="true" />
          <span>清除所有会话</span>
        </button>
      )}
    </div>
  );
}
