import { memo } from 'react';
import { CheckSquare, Edit2, Loader2, MessageSquare, Pin, PinOff, Square, Trash2 } from 'lucide-react';

import type { SessionRecord } from '../types';

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
 * 侧边栏里的单个会话条目。
 *
 * O-6：改动前整行是一个 `div[role="button"][tabIndex=0]`，内部又嵌了真实的
 * `<button>`（勾选 / 置顶 / 重命名 / 删除）。在被暴露为 button 的元素内放交互后代
 * 是无效结构——辅助技术会压平或误报，而且同一行有两条键盘激活路径。
 *
 * 现在外层只是普通容器，只有一个真正的交互元素负责「选中该会话」。
 *
 * memo 化 + 回调 props 全部由 Sidebar 用 useCallback 固定引用：run 起止只改变
 * `isRunning` 这个布尔值，不能让 n 条会话全部重渲染（O-1 的后续）。
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
      className={`group relative flex items-center gap-2 px-3 py-2 rounded-md text-xs transition-colors ${
        isSelected
          ? 'bg-accent/80 text-accent-foreground font-medium ring-1 ring-ring/30'
          : isActive
            ? 'bg-accent text-accent-foreground font-medium'
            : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
      }`}
    >
      {/* 多选框 或 状态图标 */}
      {isSelectMode ? (
        <button
          type="button"
          onClick={() => onToggleSelect(session.id)}
          aria-pressed={isSelected}
          className="shrink-0 text-muted-foreground hover:text-foreground cursor-pointer"
        >
          {isSelected ? (
            <CheckSquare size={14} className="text-primary" aria-hidden="true" />
          ) : (
            <Square size={14} aria-hidden="true" />
          )}
          <span className="sr-only">
            {isSelected ? '取消选择' : '选择'}会话 {session.title}
          </span>
        </button>
      ) : isRunning ? (
        <Loader2 size={14} className="shrink-0 animate-spin text-foreground/70" aria-hidden="true" />
      ) : (
        <MessageSquare size={14} className="shrink-0" aria-hidden="true" />
      )}

      {/* 编辑模式下的文本框，或「选中该会话」按钮 */}
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
          className="flex-1 border rounded px-1.5 py-0.5 text-xs bg-background text-foreground outline-none"
        />
      ) : (
        <button
          type="button"
          onClick={handleActivate}
          aria-current={isActive ? 'true' : undefined}
          className="flex-1 truncate text-left cursor-pointer"
        >
          {session.title}
        </button>
      )}

      {/* 置顶标记常显 */}
      {isPinned && !isEditing && !isSelectMode && (
        <Pin
          size={11}
          className="shrink-0 text-muted-foreground/70 fill-muted-foreground/40 rotate-45"
          aria-hidden="true"
        />
      )}

      {/* 单个操作按钮。
          不可见时同时 pointer-events-none：此前只有 opacity-0，触屏没有 hover，
          按钮既看不见又能被点到（盲区误触），重命名实质不可达。
          现在「可见即可点、不可见不可点」：悬停/聚焦时显示，触屏（pointer: coarse）
          常显。 */}
      {!isEditing && !isSelectMode && (
        <div className="opacity-0 pointer-events-none group-hover:opacity-100 group-hover:pointer-events-auto group-focus-within:opacity-100 group-focus-within:pointer-events-auto pointer-coarse:opacity-100 pointer-coarse:pointer-events-auto flex items-center gap-0.5 transition-opacity shrink-0">
          <button
            type="button"
            onClick={() => trigger(() => onTogglePin(session.id), '切换置顶失败')}
            title={isPinned ? '取消置顶' : '置顶对话'}
            className="p-1 hover:text-foreground text-muted-foreground rounded transition cursor-pointer"
          >
            {isPinned ? <PinOff size={12} aria-hidden="true" /> : <Pin size={12} aria-hidden="true" />}
            <span className="sr-only">{isPinned ? '取消置顶' : '置顶'}会话 {session.title}</span>
          </button>
          <button
            type="button"
            onClick={() => onStartEdit(session)}
            title="重命名"
            className="p-1 hover:text-foreground text-muted-foreground rounded transition cursor-pointer"
          >
            <Edit2 size={12} aria-hidden="true" />
            <span className="sr-only">重命名会话 {session.title}</span>
          </button>
          <button
            type="button"
            onClick={() => onRequestDelete(session)}
            title="删除"
            className="p-1 hover:text-destructive text-muted-foreground rounded transition cursor-pointer"
          >
            <Trash2 size={12} aria-hidden="true" />
            <span className="sr-only">删除会话 {session.title}</span>
          </button>
        </div>
      )}
    </div>
  );
});
