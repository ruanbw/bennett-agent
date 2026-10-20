import { useCallback, useRef, useState } from 'react';
import { Check, Pencil, Plus, X } from 'lucide-react';

import { Thread } from './assistant-ui/elements/thread.aui';
import { ChatSessionNotices } from './ChatProvider';
import type { ModelItem } from '../types';

export interface ChatContainerProps {
  sessionTitle?: string;
  onUpdateTitle?: (title: string) => void | Promise<unknown>;
  models: ModelItem[];
  modelsError?: string | null;
  currentModel: string | null;
  currentReasoningEffort?: string;
  onSelectModel: (modelId: string) => void;
  onSelectReasoningEffort?: (effort: string) => void;
  onRefreshModels?: () => void | Promise<unknown>;
  onToggleSidebar: () => void;
  onCreateSession?: () => void | Promise<unknown>;
  /** 侧边栏是否展开，决定是否展示左上角胶囊工具栏 */
  isSidebarOpen: boolean;
}

/**
 * 对话主区域：顶栏（胶囊工具栏 + 可编辑标题）+ Thread。
 *
 * 当侧边栏折叠时，左上角出现仿 DeepSeek 的胶囊药丸工具栏（Pill Toolbar），
 * 包含：展开侧边栏、搜索、新对话 三个图标按钮，带细边框和微阴影。
 */
export function ChatContainer({
  sessionTitle,
  onUpdateTitle,
  models,
  modelsError,
  currentModel,
  currentReasoningEffort,
  onSelectModel,
  onSelectReasoningEffort,
  onRefreshModels,
  onToggleSidebar,
  onCreateSession,
  isSidebarOpen,
}: ChatContainerProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [editingTitle, setEditingTitle] = useState('');
  const [prevSessionTitle, setPrevSessionTitle] = useState(sessionTitle);
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelEditRef = useRef(false);

  if (prevSessionTitle !== sessionTitle) {
    setPrevSessionTitle(sessionTitle);
    if (!isEditing) {
      setEditingTitle(sessionTitle || '新对话');
    }
  }

  const handleStartEdit = useCallback(() => {
    cancelEditRef.current = false;
    setEditingTitle(sessionTitle || '新对话');
    setIsEditing(true);
  }, [sessionTitle]);

  const handleSaveTitle = useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault();
      if (cancelEditRef.current) {
        cancelEditRef.current = false;
        setIsEditing(false);
        return;
      }
      const trimmed = editingTitle.trim();
      setIsEditing(false);
      if (!trimmed || trimmed === sessionTitle) return;
      try {
        await onUpdateTitle?.(trimmed);
      } catch (err) {
        console.error('修改会话标题失败:', err);
      }
    },
    [editingTitle, sessionTitle, onUpdateTitle]
  );

  const handleCancelEdit = useCallback(() => {
    cancelEditRef.current = true;
    setIsEditing(false);
    setEditingTitle(sessionTitle || '新对话');
  }, [sessionTitle]);

  return (
    <div className="flex-1 flex flex-col h-full bg-background text-foreground overflow-hidden">
      <header className="h-12 flex items-center justify-between px-3 z-10 shrink-0 select-none">
        <div className="flex items-center gap-3 min-w-0 flex-1">
          {/* 侧边栏折叠时：左上角胶囊药丸工具栏（仿 DeepSeek 收缩态） */}
          {!isSidebarOpen && (
            <div className="flex items-center border border-border/60 rounded-xl shadow-xs bg-background overflow-hidden shrink-0">
              <button
                type="button"
                onClick={onToggleSidebar}
                className="size-8 flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent/50 transition cursor-pointer"
                title="展开侧边栏"
                aria-label="展开侧边栏"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <line x1="9" y1="3" x2="9" y2="21" />
                </svg>
              </button>
              {onCreateSession && (
                <button
                  type="button"
                  onClick={onCreateSession}
                  className="size-8 flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent/50 transition cursor-pointer"
                  title="新建对话"
                  aria-label="新建对话"
                >
                  <Plus size={16} aria-hidden="true" />
                </button>
              )}
            </div>
          )}

          {/* 可编辑的会话标题 */}
          {isEditing ? (
            <form onSubmit={handleSaveTitle} className="flex items-center gap-1 min-w-0">
              <input
                ref={inputRef}
                type="text"
                value={editingTitle}
                onChange={(e) => setEditingTitle(e.target.value)}
                onBlur={handleSaveTitle}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') handleCancelEdit();
                }}
                className="h-7 px-2.5 text-sm font-medium bg-background border border-ring/40 rounded-lg outline-none focus:ring-1 focus:ring-ring/40 text-foreground w-48 sm:w-72"
                maxLength={100}
                placeholder="输入会话标题..."
                autoFocus
              />
              <button
                type="submit"
                className="p-1 text-muted-foreground hover:text-foreground rounded-md transition cursor-pointer"
                title="保存"
                aria-label="保存"
              >
                <Check className="size-3.5 text-primary" />
              </button>
              <button
                type="button"
                onMouseDown={handleCancelEdit}
                className="p-1 text-muted-foreground hover:text-foreground rounded-md transition cursor-pointer"
                title="取消"
                aria-label="取消"
              >
                <X className="size-3.5" />
              </button>
            </form>
          ) : (
            /*
              标题本身是重命名的入口，必须是可聚焦的 button：
              之前是 div onClick，键盘用户只能看到标题、改不了名字；触屏上铅笔图标又被
              `group-hover` 隐藏（触屏没有 hover），所幸点击标题仍生效，所以只修语义即可。
            */
            <button
              type="button"
              onClick={handleStartEdit}
              title="点击修改会话标题"
              aria-label={`重命名会话标题：${sessionTitle || '新对话'}`}
              className="group flex items-center gap-1.5 max-w-[50vw] sm:max-w-md px-2 py-1 rounded-lg hover:bg-muted/50 transition cursor-pointer min-w-0 outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
            >
              <span className="text-sm font-medium text-foreground truncate select-none">
                {sessionTitle || '新对话'}
              </span>
              <Pencil
                className="size-3 text-muted-foreground opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 transition shrink-0"
                aria-hidden="true"
              />
            </button>
          )}
        </div>
      </header>

      <ChatSessionNotices />

      <div className="flex-1 overflow-hidden">
        <Thread
          models={models}
          modelsError={modelsError}
          currentModel={currentModel}
          currentReasoningEffort={currentReasoningEffort}
          onSelectModel={onSelectModel}
          onSelectReasoningEffort={onSelectReasoningEffort}
          onRefreshModels={onRefreshModels}
          onCreateSession={onCreateSession}
        />
      </div>
    </div>
  );
}
