import { memo } from 'react';
import { Loader2, MoreHorizontal, Pin, PinOff, Edit2, Trash2 } from 'lucide-react';

import type { SessionRecord } from '../types';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';

/** 触发可能返回 Promise 的回调并接住 rejection：不产生 unhandled rejection。 */
function trigger(action: () => void | Promise<unknown>, label: string): void {
  void Promise.resolve(action()).catch((err: unknown) => console.error(`${label}:`, err));
}

export interface SidebarSessionItemProps {
  session: SessionRecord;
  isActive: boolean;
  isRunning: boolean;
  isSelected: boolean;
  isSelectMode: boolean;
  isEditing: boolean;
  editingTitle: string;
  onEditingTitleChange: (title: string) => void;
  onStartEdit: (session: SessionRecord) => void;
  onSaveEdit: (sessionId: string) => void;
  onCancelEdit: () => void;
  onSelect: (id: string) => void;
  onToggleSelect: (id: string) => void;
  onTogglePin: (id: string) => void | Promise<unknown>;
  onRequestDelete: (session: SessionRecord) => void;
}

/**
 * 侧边栏单个会话条目 — 参照 DeepSeek 极简风格重构。
 *
 * - 去掉 MessageSquare / CheckSquare 等左侧图标噪音，只保留文字标题
 * - hover 时右侧浮现三点操作菜单（MoreHorizontal），点击弹出 DropdownMenu
 * - 运行中的会话左侧展示小转圈 Loader2
 * - 置顶标记只用小 Pin 角标
 */
export const SidebarSessionItem = memo(function SidebarSessionItem({
  session,
  isActive,
  isRunning,
  isSelected,
  isSelectMode,
  isEditing,
  editingTitle,
  onEditingTitleChange,
  onStartEdit,
  onSaveEdit,
  onCancelEdit,
  onSelect,
  onToggleSelect,
  onTogglePin,
  onRequestDelete,
}: SidebarSessionItemProps) {
  const isPinned = Boolean(session.isPinned);

  const handleActivate = () => {
    if (isSelectMode) onToggleSelect(session.id);
    else onSelect(session.id);
  };

  return (
    <div
      className={`group relative flex items-center gap-2 px-3 py-2.5 rounded-xl text-[13px] transition-colors cursor-pointer ${
        isSelected
          ? 'bg-accent/80 text-accent-foreground font-medium'
          : isActive
            ? 'bg-accent/70 text-foreground font-medium'
            : 'text-foreground/80 hover:bg-accent/40'
      }`}
      onClick={isEditing ? undefined : handleActivate}
    >
      {/* 运行中指示器 */}
      {isRunning && (
        <Loader2 size={14} className="shrink-0 animate-spin text-foreground/60" aria-hidden="true" />
      )}

      {/* 编辑模式 / 正常展示 */}
      {isEditing ? (
        <input
          type="text"
          value={editingTitle}
          onChange={(event) => onEditingTitleChange(event.target.value)}
          onBlur={() => onSaveEdit(session.id)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              onSaveEdit(session.id);
            }
            if (event.key === 'Escape') {
              event.preventDefault();
              onCancelEdit();
            }
          }}
          aria-label="重命名会话"
          autoFocus
          className="flex-1 border border-ring/40 rounded-lg px-2 py-0.5 text-[13px] bg-background text-foreground outline-none focus:ring-1 focus:ring-ring/40"
        />
      ) : (
        <span className="flex-1 truncate select-none">
          {session.title || '新对话'}
        </span>
      )}

      {/* 置顶角标 */}
      {isPinned && !isEditing && !isSelectMode && (
        <Pin
          size={10}
          className="shrink-0 text-muted-foreground/50 fill-muted-foreground/30 rotate-45"
          aria-hidden="true"
        />
      )}

      {/* hover 时浮现三点操作菜单 */}
      {!isEditing && !isSelectMode && (
        <div
          className="opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto transition-opacity shrink-0"
          onClick={(e) => e.stopPropagation()}
        >
          <DropdownMenu>
            <DropdownMenuTrigger
              className="size-6 flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent rounded-md transition cursor-pointer outline-none"
              aria-label="会话操作"
            >
              <MoreHorizontal size={14} />
            </DropdownMenuTrigger>
            <DropdownMenuContent side="right" align="start" sideOffset={4}>
              <DropdownMenuItem onClick={() => onStartEdit(session)}>
                <Edit2 size={14} className="text-muted-foreground" />
                <span>重命名</span>
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => trigger(() => onTogglePin(session.id), '切换置顶失败')}
              >
                {isPinned ? (
                  <PinOff size={14} className="text-muted-foreground" />
                ) : (
                  <Pin size={14} className="text-muted-foreground" />
                )}
                <span>{isPinned ? '取消置顶' : '置顶对话'}</span>
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onClick={() => onRequestDelete(session)}
                className="text-destructive hover:bg-destructive/10 data-[highlighted]:bg-destructive/10 data-[highlighted]:text-destructive"
              >
                <Trash2 size={14} />
                <span>删除</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}
    </div>
  );
});
