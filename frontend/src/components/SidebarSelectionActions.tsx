import { Download, Pin, PinOff, Trash2, type LucideIcon } from 'lucide-react';

export interface SidebarSelectionActionsProps {
  selectedCount: number;
  /** 选中的会话是否全部已置顶：决定「置顶」按钮做哪件事，避免并排两个互斥按钮。 */
  allPinned: boolean;
  onTogglePin: () => void | Promise<unknown>;
  /** 把选中会话导出为一个 Markdown 归档；未传时不渲染（不给出点不动的入口）。 */
  onExportSelected?: () => void | Promise<unknown>;
  onRequestDelete: () => void | Promise<unknown>;
}

/** 触发可能返回 Promise 的回调并接住 rejection：不产生 unhandled rejection。 */
function trigger(action: () => void | Promise<unknown>, label: string): void {
  void Promise.resolve(action()).catch((err: unknown) => console.error(`${label}:`, err));
}

function ActionButton({
  icon: Icon,
  label,
  onClick,
  disabled,
  destructive = false,
  actionLabel,
}: {
  icon: LucideIcon;
  label: string;
  onClick: () => void | Promise<unknown>;
  disabled: boolean;
  destructive?: boolean;
  actionLabel: string;
}) {
  return (
    <button
      type="button"
      onClick={() => trigger(onClick, actionLabel)}
      disabled={disabled}
      title={label}
      className={`flex h-9 flex-1 items-center justify-center gap-1.5 rounded-lg text-[12px] transition-colors outline-none cursor-pointer focus-visible:ring-1 focus-visible:ring-ring/60 disabled:pointer-events-none disabled:opacity-40 ${
        destructive
          ? 'text-destructive hover:bg-destructive/10'
          : 'text-foreground/75 hover:bg-accent hover:text-foreground'
      }`}
    >
      <Icon size={14} aria-hidden="true" />
      <span className="whitespace-nowrap">{label}</span>
    </button>
  );
}

/**
 * 多选模式的底部操作条。
 *
 * 占据账号区的位置：批量操作是这一刻的主任务，账号区让位比在列表上方再叠一张卡片更
 * 省空间，拇指也更容易够到（移动端抽屉里尤其明显）。
 *
 * 「置顶 / 取消置顶」合成一个按钮：两者互斥，并排摆两个永远是其中一个没意义。
 * 「删除」单独染成破坏色，且确认弹窗在按下之后才出现——误触代价最高的一项不该和
 * 另外两项长得一样。
 */
export function SidebarSelectionActions({
  selectedCount,
  allPinned,
  onTogglePin,
  onExportSelected,
  onRequestDelete,
}: SidebarSelectionActionsProps) {
  const isEmpty = selectedCount === 0;
  const pinLabel = allPinned ? '取消置顶' : '置顶';

  return (
    <div
      role="group"
      aria-label={`批量操作，已选 ${selectedCount} 项`}
      className="flex items-center gap-1 border-t border-border/60 px-2 py-1.5"
    >
      <ActionButton
        icon={allPinned ? PinOff : Pin}
        label={pinLabel}
        onClick={onTogglePin}
        disabled={isEmpty}
        actionLabel={`${pinLabel}选中会话失败`}
      />
      {onExportSelected && (
        <ActionButton
          icon={Download}
          label="导出"
          onClick={onExportSelected}
          disabled={isEmpty}
          actionLabel="导出选中会话失败"
        />
      )}
      <ActionButton
        icon={Trash2}
        label="删除"
        onClick={onRequestDelete}
        disabled={isEmpty}
        destructive
        actionLabel="批量删除失败"
      />
    </div>
  );
}
