import React, { useMemo, useRef, useState, useSyncExternalStore } from 'react';
import {
  Plus,
  MessageSquare,
  Loader2,
  Edit2,
  Trash2,
  Sun,
  Moon,
  Laptop,
  Pin,
  PinOff,
  Search,
  CheckSquare,
  Square,
  X,
  SlidersHorizontal,
} from 'lucide-react';
import { getRunningSessionIds, subscribeRuns } from '../lib/chat-runs';
import { useTheme } from '../hooks/use-theme';

export function Sidebar({
  sessions,
  currentSessionId,
  onSelectSession,
  onCreateSession,
  onUpdateTitle,
  onDeleteSession,
  onTogglePinSession,
  onBatchDelete,
  onBatchTogglePin,
  onClearAllSessions,
  isOpen,
}) {
  const [editingId, setEditingId] = useState(null);
  const [editingTitle, setEditingTitle] = useState('');
  // 标记本次编辑是取消：避免卸载/blur 时又触发一次保存
  const cancelEditRef = useRef(false);

  // 搜索关键字
  const [searchQuery, setSearchQuery] = useState('');
  // 多选管理模式
  const [isSelectMode, setIsSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState(new Set());

  // 生成中的会话（快照是拼接字符串，集合不变时不会触发重渲染）
  const runningKey = useSyncExternalStore(subscribeRuns, () =>
    getRunningSessionIds().join(',')
  );
  const runningIds = runningKey ? runningKey.split(',') : [];

  const startEdit = (session) => {
    cancelEditRef.current = false;
    setEditingId(session.id);
    setEditingTitle(session.title);
  };

  const saveEdit = (sessionId) => {
    if (cancelEditRef.current) {
      cancelEditRef.current = false;
      setEditingId(null);
      return;
    }
    if (editingTitle.trim()) {
      onUpdateTitle(sessionId, editingTitle.trim());
    }
    setEditingId(null);
  };

  const cancelEdit = () => {
    cancelEditRef.current = true;
    setEditingId(null);
  };

  const { theme, setTheme } = useTheme();

  // 根据搜索关键字过滤会话
  const filteredSessions = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter((s) => (s.title || '').toLowerCase().includes(q));
  }, [sessions, searchQuery]);

  // 多选相关逻辑
  const toggleSelect = (id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const selectAll = () => {
    const allFiltered = filteredSessions.map((s) => s.id);
    setSelectedIds(new Set(allFiltered));
  };

  const deselectAll = () => {
    setSelectedIds(new Set());
  };

  const exitSelectMode = () => {
    setIsSelectMode(false);
    setSelectedIds(new Set());
  };

  const handleBatchDeleteClick = () => {
    if (selectedIds.size === 0) return;
    if (confirm(`确认删除选中的 ${selectedIds.size} 个对话吗？`)) {
      onBatchDelete(Array.from(selectedIds));
      setSelectedIds(new Set());
      setIsSelectMode(false);
    }
  };

  const handleBatchPinClick = (pinState) => {
    if (selectedIds.size === 0) return;
    onBatchTogglePin(Array.from(selectedIds), pinState);
    setSelectedIds(new Set());
  };

  const handleClearAllClick = () => {
    if (confirm('警告：此操作将清空所有历史对话记录，且无法恢复。是否确认清除全部会话？')) {
      onClearAllSessions();
      setIsSelectMode(false);
      setSelectedIds(new Set());
    }
  };

  const isAllSelected =
    filteredSessions.length > 0 &&
    filteredSessions.every((s) => selectedIds.has(s.id));

  return (
    <aside
      className={`w-64 border-r flex flex-col h-full select-none transition-transform duration-200 z-30 shrink-0 bg-muted/20 absolute md:relative inset-y-0 left-0 ${
        isOpen ? 'translate-x-0' : '-translate-x-full md:hidden'
      }`}
    >
      {/* 头部：新建对话 & 搜索框与多选管理按钮 */}
      <div className="p-3 pb-2 space-y-2">
        <button
          onClick={onCreateSession}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 text-xs font-medium rounded-md border hover:bg-accent hover:text-accent-foreground transition cursor-pointer"
        >
          <Plus size={14} />
          <span>新对话</span>
        </button>

        {/* 搜索框与右侧多选按钮 */}
        <div className="flex items-center gap-1.5">
          <div className="relative flex-1 flex items-center">
            <Search
              size={13}
              className="absolute left-2.5 text-muted-foreground pointer-events-none"
            />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="搜索历史会话..."
              className="w-full bg-background border rounded-md pl-8 pr-7 py-1.5 text-xs text-foreground placeholder:text-muted-foreground outline-none focus:border-ring/50"
            />
            {searchQuery && (
              <button
                onClick={() => setSearchQuery('')}
                className="absolute right-2 text-muted-foreground hover:text-foreground p-0.5 cursor-pointer"
                title="清除搜索"
              >
                <X size={12} />
              </button>
            )}
          </div>

          <button
            type="button"
            onClick={() => {
              setIsSelectMode((v) => !v);
              setSelectedIds(new Set());
            }}
            title={isSelectMode ? '退出多选' : '多选管理'}
            className={`p-1.5 rounded-md transition cursor-pointer text-xs shrink-0 ${
              isSelectMode
                ? 'bg-accent text-accent-foreground'
                : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
            }`}
          >
            <SlidersHorizontal size={14} />
          </button>
        </div>
      </div>

      {/* 批量操作控制栏 */}
      {isSelectMode && (
        <div className="px-3 py-1.5 bg-muted/60 text-xs flex flex-col gap-1.5">
          <div className="flex items-center justify-between text-muted-foreground">
            <span className="font-medium text-foreground">
              已选 {selectedIds.size} 项
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={isAllSelected ? deselectAll : selectAll}
                className="hover:text-foreground cursor-pointer underline text-[11px]"
              >
                {isAllSelected ? '取消全选' : '全选'}
              </button>
              <button
                onClick={exitSelectMode}
                className="hover:text-foreground cursor-pointer text-[11px]"
              >
                完成
              </button>
            </div>
          </div>

          <div className="flex items-center gap-1.5 pt-1">
            <button
              onClick={() => handleBatchPinClick(true)}
              disabled={selectedIds.size === 0}
              className="flex-1 flex items-center justify-center gap-1 py-1 px-1.5 rounded bg-background hover:bg-accent text-[11px] disabled:opacity-50 cursor-pointer"
              title="置顶选中会话"
            >
              <Pin size={11} />
              <span>置顶</span>
            </button>
            <button
              onClick={() => handleBatchPinClick(false)}
              disabled={selectedIds.size === 0}
              className="flex-1 flex items-center justify-center gap-1 py-1 px-1.5 rounded bg-background hover:bg-accent text-[11px] disabled:opacity-50 cursor-pointer"
              title="取消置顶"
            >
              <PinOff size={11} />
              <span>取消</span>
            </button>
            <button
              onClick={handleBatchDeleteClick}
              disabled={selectedIds.size === 0}
              className="flex-1 flex items-center justify-center gap-1 py-1 px-1.5 rounded bg-destructive/10 text-destructive hover:bg-destructive/20 text-[11px] disabled:opacity-50 cursor-pointer"
              title="删除选中"
            >
              <Trash2 size={11} />
              <span>删除</span>
            </button>
          </div>
        </div>
      )}

      {/* 会话列表 */}
      <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
        {filteredSessions.length === 0 ? (
          <div className="text-xs text-muted-foreground text-center py-8">
            {searchQuery ? '未找到相关对话' : '暂无历史对话'}
          </div>
        ) : (
          filteredSessions.map((session) => {
            const isPinned = Boolean(session.isPinned);
            const isSelected = selectedIds.has(session.id);

            return (
              <div
                key={session.id}
                onClick={() => {
                  if (isSelectMode) {
                    toggleSelect(session.id);
                  } else {
                    onSelectSession(session.id);
                  }
                }}
                className={`group relative flex items-center gap-2 px-3 py-2 rounded-md text-xs cursor-pointer transition-colors ${
                  isSelected
                    ? 'bg-accent/80 text-accent-foreground font-medium ring-1 ring-ring/30'
                    : currentSessionId === session.id && !isSelectMode
                    ? 'bg-accent text-accent-foreground font-medium'
                    : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
                }`}
              >
                {/* 多选框 或 状态图标 */}
                {isSelectMode ? (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleSelect(session.id);
                    }}
                    className="shrink-0 text-muted-foreground hover:text-foreground"
                  >
                    {isSelected ? (
                      <CheckSquare size={14} className="text-primary" />
                    ) : (
                      <Square size={14} />
                    )}
                  </button>
                ) : runningIds.includes(session.id) ? (
                  <Loader2 size={14} className="shrink-0 animate-spin text-foreground/70" />
                ) : (
                  <MessageSquare size={14} className="shrink-0" />
                )}

                {/* 编辑模式下的文本框 */}
                {editingId === session.id ? (
                  <input
                    type="text"
                    value={editingTitle}
                    onChange={(e) => setEditingTitle(e.target.value)}
                    onBlur={() => saveEdit(session.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        saveEdit(session.id);
                      }
                      if (e.key === 'Escape') {
                        e.preventDefault();
                        cancelEdit();
                      }
                    }}
                    autoFocus
                    className="flex-1 border rounded px-1.5 py-0.5 text-xs bg-background text-foreground outline-none"
                  />
                ) : (
                  <span className="flex-1 truncate flex items-center gap-1.5">
                    {session.title}
                  </span>
                )}

                {/* 置顶图标常显标记 */}
                {isPinned && editingId !== session.id && !isSelectMode && (
                  <Pin
                    size={11}
                    className="shrink-0 text-muted-foreground/70 fill-muted-foreground/40 rotate-45"
                  />
                )}

                {/* 单个操作按钮 */}
                {editingId !== session.id && !isSelectMode && (
                  <div className="opacity-0 group-hover:opacity-100 flex items-center gap-0.5 transition-opacity shrink-0">
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        onTogglePinSession(session.id);
                      }}
                      title={isPinned ? '取消置顶' : '置顶对话'}
                      className="p-1 hover:text-foreground text-muted-foreground rounded transition cursor-pointer"
                    >
                      {isPinned ? <PinOff size={12} /> : <Pin size={12} />}
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        startEdit(session);
                      }}
                      title="重命名"
                      className="p-1 hover:text-foreground text-muted-foreground rounded transition cursor-pointer"
                    >
                      <Edit2 size={12} />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirm('确认删除此对话记录吗？')) {
                          onDeleteSession(session.id);
                        }
                      }}
                      title="删除"
                      className="p-1 hover:text-destructive text-muted-foreground rounded transition cursor-pointer"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>

      {/* 底部功能区：清空全部会话（仅多选模式展示） & 主题切换 */}
      <div className="p-3 space-y-2">
        {isSelectMode && sessions.length > 0 && (
          <button
            onClick={handleClearAllClick}
            className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-[11px] text-destructive hover:bg-destructive/10 rounded-md transition cursor-pointer"
            title="清空所有会话记录"
          >
            <Trash2 size={12} />
            <span>清除所有会话</span>
          </button>
        )}

        <div className="flex items-center justify-between bg-muted/60 p-1 rounded-lg border border-border/40">
          <button
            type="button"
            onClick={() => setTheme('light')}
            title="明亮模式"
            aria-label="明亮模式"
            aria-pressed={theme === 'light'}
            className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 px-2 rounded-md text-xs font-medium transition cursor-pointer ${
              theme === 'light'
                ? 'bg-background text-foreground shadow-xs'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            <Sun size={14} className={theme === 'light' ? 'text-amber-500' : ''} />
            <span>浅色</span>
          </button>

          <button
            type="button"
            onClick={() => setTheme('system')}
            title="跟随系统"
            aria-label="跟随系统"
            aria-pressed={theme === 'system'}
            className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 px-2 rounded-md text-xs font-medium transition cursor-pointer ${
              theme === 'system'
                ? 'bg-background text-foreground shadow-xs'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            <Laptop size={14} className={theme === 'system' ? 'text-foreground' : ''} />
            <span>系统</span>
          </button>

          <button
            type="button"
            onClick={() => setTheme('dark')}
            title="暗黑模式"
            aria-label="暗黑模式"
            aria-pressed={theme === 'dark'}
            className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 px-2 rounded-md text-xs font-medium transition cursor-pointer ${
              theme === 'dark'
                ? 'bg-background text-foreground shadow-xs'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            <Moon size={14} className={theme === 'dark' ? 'text-sky-400' : ''} />
            <span>深色</span>
          </button>
        </div>
      </div>
    </aside>
  );
}

