import { useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Plus, Search, SlidersHorizontal, X } from 'lucide-react';

import type { SessionRecord } from '../types';
import { getRunningSessionsKey, subscribeRunningSessions } from '../lib/chat-runs';
import { AccountBar } from './AccountBar';
import { ConfirmDialog } from './ui/confirm-dialog';
import { SidebarBatchBar } from './SidebarBatchBar';
import { SidebarSessionItem } from './SidebarSessionItem';
import { ThemeSwitcher } from './ThemeSwitcher';

/** 破坏性操作的确认请求：调用方以状态持有「确认后做什么」，ConfirmDialog 只负责呈现。 */
export interface ConfirmRequest {
  title: string;
  description: string;
  confirmLabel: string;
  destructive: boolean;
  onConfirm: () => void;
}

export interface SidebarProps {
  sessions: SessionRecord[];
  currentSessionId: string | null;
  onSelectSession: (id: string) => void;
  // 回调一律放宽到 `void | Promise<unknown>`：消费方既有同步实现也有 async 实现
  onCreateSession: () => void | Promise<unknown>;
  onUpdateTitle: (id: string, title: string) => void | Promise<unknown>;
  onDeleteSession: (id: string) => void;
  onTogglePinSession: (id: string) => void;
  onBatchDelete: (ids: string[]) => void;
  onBatchTogglePin: (ids: string[], pinned: boolean) => void;
  onClearAllSessions: () => void;
  isOpen: boolean;
  /** 当前登录用户邮箱，展示在底部账号区。 */
  userEmail: string | null;
  onLogout: () => void | Promise<unknown>;
}

/**
 * 历史会话侧边栏：搜索、单条操作、多选批量操作、主题切换。
 *
 * 本组件只保留「列表 + 选择状态」这一件事，条目、批量栏与主题切换各自独立成组件
 * （原文件 463 行，同时承担五件事）。
 *
 * O-1：订阅的是「哪些会话在跑」这个集合的快照，而不是全局增量版本——
 * 流式输出不会让侧边栏重渲染。
 * O-5：破坏性操作改用 Dialog，不再用 `window.confirm()`。
 */
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
  userEmail,
  onLogout,
}: SidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');
  // 标记本次编辑是取消：避免卸载/blur 时又触发一次保存
  const cancelEditRef = useRef(false);

  const [searchQuery, setSearchQuery] = useState('');

  const [isSelectMode, setIsSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());

  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null);

  const runningKey = useSyncExternalStore(subscribeRunningSessions, getRunningSessionsKey);
  const runningIds = runningKey ? runningKey.split(',') : [];

  const filteredSessions = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) return sessions;
    return sessions.filter((session) => (session.title || '').toLowerCase().includes(query));
  }, [sessions, searchQuery]);

  // -------------------------------------------------------------------
  // 重命名
  // -------------------------------------------------------------------
  const startEdit = (session: SessionRecord) => {
    cancelEditRef.current = false;
    setEditingId(session.id);
    setEditingTitle(session.title);
  };

  const saveEdit = (sessionId: string) => {
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

  // -------------------------------------------------------------------
  // 多选
  // -------------------------------------------------------------------
  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const exitSelectMode = () => {
    setIsSelectMode(false);
    setSelectedIds(new Set());
  };

  const isAllSelected =
    filteredSessions.length > 0 && filteredSessions.every((session) => selectedIds.has(session.id));

  const selectedArray = (): string[] => Array.from(selectedIds);

  // -------------------------------------------------------------------
  // 破坏性操作：先弹确认，再执行
  // -------------------------------------------------------------------
  const requestDeleteSession = (session: SessionRecord) => {
    setConfirmRequest({
      title: '删除对话',
      description: `「${session.title}」及其全部消息将被删除，且无法恢复。`,
      confirmLabel: '删除',
      destructive: true,
      onConfirm: () => onDeleteSession(session.id),
    });
  };

  const requestBatchDelete = () => {
    if (selectedIds.size === 0) return;
    const ids = selectedArray();
    setConfirmRequest({
      title: `删除 ${ids.length} 个对话`,
      description: '选中会话及其全部消息将被删除，且无法恢复。',
      confirmLabel: '删除',
      destructive: true,
      onConfirm: () => {
        onBatchDelete(ids);
        exitSelectMode();
      },
    });
  };

  const requestClearAll = () => {
    setConfirmRequest({
      title: '清空所有历史对话',
      description: '全部会话与消息将被删除，且无法恢复。',
      confirmLabel: '全部清空',
      destructive: true,
      onConfirm: () => {
        onClearAllSessions();
        exitSelectMode();
      },
    });
  };

  return (
    <aside
      aria-label="历史对话侧边栏"
      // React 19 的 inert 是布尔属性；此前传的是 `''`，语义上等值但不符合类型约定
      inert={!isOpen}
      className={`w-64 border-r flex flex-col h-full select-none transition-transform duration-200 z-30 shrink-0 bg-muted/20 absolute md:relative inset-y-0 left-0 ${
        isOpen ? 'translate-x-0' : '-translate-x-full md:hidden pointer-events-none'
      }`}
    >
      <div className="p-3 pb-2 space-y-2">
        <button
          type="button"
          onClick={onCreateSession}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 text-xs font-medium rounded-md border hover:bg-accent hover:text-accent-foreground transition cursor-pointer"
        >
          <Plus size={14} aria-hidden="true" />
          <span>新对话</span>
        </button>

        <div className="flex items-center gap-1.5">
          <div className="relative flex-1 flex items-center">
            <Search
              size={13}
              className="absolute left-2.5 text-muted-foreground pointer-events-none"
              aria-hidden="true"
            />
            <input
              type="text"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="搜索历史会话..."
              aria-label="搜索历史会话"
              className="w-full bg-background border rounded-md pl-8 pr-7 py-1.5 text-xs text-foreground placeholder:text-muted-foreground outline-none focus:border-ring/50"
            />
            {searchQuery && (
              <button
                type="button"
                onClick={() => setSearchQuery('')}
                className="absolute right-2 text-muted-foreground hover:text-foreground p-0.5 cursor-pointer"
                title="清除搜索"
              >
                <X size={12} aria-hidden="true" />
                <span className="sr-only">清除搜索</span>
              </button>
            )}
          </div>

          <button
            type="button"
            onClick={() => {
              setIsSelectMode((value) => !value);
              setSelectedIds(new Set());
            }}
            title={isSelectMode ? '退出多选' : '多选管理'}
            aria-pressed={isSelectMode}
            className={`p-1.5 rounded-md transition cursor-pointer text-xs shrink-0 ${
              isSelectMode
                ? 'bg-accent text-accent-foreground'
                : 'text-muted-foreground hover:text-foreground hover:bg-accent/60'
            }`}
          >
            <SlidersHorizontal size={14} aria-hidden="true" />
            <span className="sr-only">{isSelectMode ? '退出多选' : '多选管理'}</span>
          </button>
        </div>
      </div>

      {isSelectMode && (
        <SidebarBatchBar
          selectedCount={selectedIds.size}
          isAllSelected={isAllSelected}
          onToggleSelectAll={() =>
            setSelectedIds(isAllSelected ? new Set() : new Set(filteredSessions.map((s) => s.id)))
          }
          onExitSelectMode={exitSelectMode}
          onPinSelected={() => {
            onBatchTogglePin(selectedArray(), true);
            setSelectedIds(new Set());
          }}
          onUnpinSelected={() => {
            onBatchTogglePin(selectedArray(), false);
            setSelectedIds(new Set());
          }}
          onRequestBatchDelete={requestBatchDelete}
          onRequestClearAll={requestClearAll}
          canClearAll={sessions.length > 0}
        />
      )}

      <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
        {filteredSessions.length === 0 ? (
          <div className="text-xs text-muted-foreground text-center py-8">
            {searchQuery ? '未找到相关对话' : '暂无历史对话'}
          </div>
        ) : (
          filteredSessions.map((session) => (
            <SidebarSessionItem
              key={session.id}
              session={session}
              isActive={currentSessionId === session.id && !isSelectMode}
              isRunning={runningIds.includes(session.id)}
              isSelected={selectedIds.has(session.id)}
              isSelectMode={isSelectMode}
              isEditing={editingId === session.id}
              editingTitle={editingTitle}
              onEditingTitleChange={setEditingTitle}
              onStartEdit={startEdit}
              onSaveEdit={() => saveEdit(session.id)}
              onCancelEdit={cancelEdit}
              onSelect={onSelectSession}
              onToggleSelect={toggleSelect}
              onTogglePin={onTogglePinSession}
              onRequestDelete={requestDeleteSession}
            />
          ))
        )}
      </div>

      <div className="p-3">
        <ThemeSwitcher />
      </div>

      <AccountBar email={userEmail} onLogout={onLogout} />

      <ConfirmDialog
        open={Boolean(confirmRequest)}
        onOpenChange={(open) => {
          if (!open) setConfirmRequest(null);
        }}
        title={confirmRequest?.title ?? ''}
        description={confirmRequest?.description}
        confirmLabel={confirmRequest?.confirmLabel}
        destructive={confirmRequest?.destructive}
        onConfirm={() => confirmRequest?.onConfirm?.()}
      />
    </aside>
  );
}
