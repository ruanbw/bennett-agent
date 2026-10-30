import { MoreHorizontal } from 'lucide-react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';

export interface SidebarSelectToolbarProps {
  selectedCount: number;
  isAllSelected: boolean;
  /** 当前可见会话是否非空：空结果时「全选」没有可操作对象，应禁用而不是点了没反应。 */
  canSelectAll: boolean;
  /** 是否还有会话可以「清除所有」；没有时不渲染该入口。 */
  canClearAll: boolean;
  onToggleSelectAll: () => void;
  onExitSelectMode: () => void;
  onRequestClearAll: () => void | Promise<unknown>;
}

/** 触发可能返回 Promise 的回调并接住 rejection：不产生 unhandled rejection。 */
function trigger(action: () => void | Promise<unknown>, label: string): void {
  void Promise.resolve(action()).catch((err: unknown) => console.error(`${label}:`, err));
}

const pillButton =
  'h-7 shrink-0 rounded-full px-2 text-[11.5px] text-muted-foreground transition-colors ' +
  'hover:bg-accent/70 hover:text-foreground cursor-pointer outline-none ' +
  'focus-visible:ring-1 focus-visible:ring-ring/60 disabled:pointer-events-none disabled:opacity-40';

/**
 * 多选模式的顶部工具条。
 *
 * 直接占据「开启新对话」胶囊的位置并沿用同一副外壳（全圆角、同高 40px）：
 * 进出多选时列表不会上下跳，视觉上像是同一个控件换了内容，而不是又叠了一张卡片。
 *
 * 三段式：左「取消」退出、中间常驻选中数、右「全选」。把原来散在卡片里的
 * 「清除所有会话」收进最右侧的溢出菜单——它是不可逆的全局操作，不该和「删除选中」
 * 挤在同一排、也不该离手指只有一次误触的距离。
 */
export function SidebarSelectToolbar({
  selectedCount,
  isAllSelected,
  canSelectAll,
  canClearAll,
  onToggleSelectAll,
  onExitSelectMode,
  onRequestClearAll,
}: SidebarSelectToolbarProps) {
  return (
    <div
      role="toolbar"
      aria-label="多选操作"
      className="grid h-10 grid-cols-[1fr_auto_1fr] items-center rounded-full border border-border/70 bg-background pl-1 pr-1"
    >
      <button
        type="button"
        onClick={onExitSelectMode}
        className={`${pillButton} justify-self-start`}
        aria-label="退出多选"
      >
        取消
      </button>

      {/* 选中数是多选模式里唯一持续变化的读数：等宽数字，避免个位数变成两位数时抖动。
          两侧列等宽（1fr），读数字因此真正落在胶囊中线上，而不是被「取消」的宽度推歪。 */}
      <span
        aria-live="polite"
        className={`truncate px-1 text-center text-[11.5px] tabular-nums ${
          selectedCount > 0 ? 'font-medium text-foreground' : 'text-muted-foreground/70'
        }`}
      >
        {selectedCount > 0 ? `已选 ${selectedCount} 项` : '选择要管理的对话'}
      </span>

      <div className="justify-self-end flex items-center gap-0.5">
        <button
          type="button"
          onClick={onToggleSelectAll}
          disabled={!canSelectAll}
          className={pillButton}
        >
          {isAllSelected ? '取消全选' : '全选'}
        </button>

        {canClearAll && (
          <DropdownMenu>
            <DropdownMenuTrigger
              className="size-7 shrink-0 rounded-full flex items-center justify-center text-muted-foreground transition-colors hover:bg-accent/70 hover:text-foreground cursor-pointer outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
              aria-label="更多批量操作"
              title="更多"
            >
              <MoreHorizontal size={14} aria-hidden="true" />
            </DropdownMenuTrigger>
            <DropdownMenuContent side="bottom" align="end" sideOffset={6}>
              <DropdownMenuItem
                onClick={() => trigger(onRequestClearAll, '清空会话失败')}
                className="text-destructive hover:bg-destructive/10 data-[highlighted]:bg-destructive/10 data-[highlighted]:text-destructive"
              >
                <span>清除所有会话</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
    </div>
  );
}
