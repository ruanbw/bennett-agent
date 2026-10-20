import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { Loader2, Plus, Search, SlidersHorizontal, X } from 'lucide-react';

import type { SessionRecord } from '../types';
import { getRunningSessionsKey, subscribeRunningSessions } from '../lib/chat-runs';
import { SEARCH_SHORTCUT_KEYS, searchShortcutLabel } from '../lib/keyboard-shortcuts';
import { matchText } from '../lib/session-search';
import { visibleSelection } from '../lib/session-list';
import { groupSessionsByDate } from '../lib/session-grouping';
import { useSessionContentSearch } from '../hooks/use-session-search';
import { AccountBar } from './AccountBar';
import { ConfirmDialog } from './ui/confirm-dialog';
import { SidebarBatchBar } from './SidebarBatchBar';
import { SidebarSessionItem } from './SidebarSessionItem';
import { SyncStatusButton } from './SyncStatusButton';

/** 破坏性操作的确认请求 */
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
  onCreateSession: () => void | Promise<unknown>;
  onUpdateTitle: (id: string, title: string) => void | Promise<unknown>;
  onDeleteSession: (id: string) => void | Promise<unknown>;
  /** 导出整段会话为 Markdown；未传时不渲染菜单项（不给出点不动的入口）。 */
  onExportSession?: (id: string) => void | Promise<unknown>;
  /** 批量导出选中的会话（一个归档文件）；未传时批量栏不渲染导出按钮。 */
  onExportSessions?: (ids: string[]) => void | Promise<unknown>;
  onTogglePinSession: (id: string) => void | Promise<unknown>;
  onBatchDelete: (ids: string[]) => void | Promise<unknown>;
  onBatchTogglePin: (ids: string[], pinned: boolean) => void | Promise<unknown>;
  onClearAllSessions: () => void | Promise<unknown>;
  isOpen: boolean;
  /** 收起侧边栏。桌面端收起后由 ChatContainer 顶栏的胶囊工具栏负责重新展开。 */
  onToggleOpen?: () => void;
  /**
   * 「打开搜索」请求：每次递增代表一次新请求（收起态没有搜索框，入口在顶栏胶囊工具栏）。
   *
   * 用计数器而不是布尔值：布尔值重复设为 true 不会产生新值，用户关掉搜索后再点一次
   * 就打不开了。
   */
  searchRequest?: number;
  userEmail: string | null;
  onLogout: () => void | Promise<unknown>;
  /** 打开设置面板（面板挂在 App，收起侧边栏后仍可由胶囊工具栏打开）。 */
  onOpenSettings: () => void;
  /** 超级管理员才渲染管理后台入口；普通用户可不传。 */
  canOpenAdmin?: boolean;
  onOpenAdmin?: () => void;
}

/**
 * 历史会话侧边栏（DeepSeek 风格）。
 *
 * 顶部是品牌名与图标，中间是胶囊「开启新对话」+ 按时间分组的会话列表，底部是账号区。
 *
 * 收起时整个 aside 不渲染（不是把宽度设成 0）：入口转移到 ChatContainer 顶栏的
 * 胶囊工具栏，避免留下一块不可见但仍参与布局 / 聚焦的空白区域。
 */
export function Sidebar({
  sessions,
  currentSessionId,
  onSelectSession,
  onCreateSession,
  onUpdateTitle,
  onDeleteSession,
  onExportSession,
  onExportSessions,
  onTogglePinSession,
  onBatchDelete,
  onBatchTogglePin,
  onClearAllSessions,
  isOpen,
  onToggleOpen,
  searchRequest = 0,
  userEmail,
  onLogout,
  onOpenSettings,
  canOpenAdmin = false,
  onOpenAdmin,
}: SidebarProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');
  // 编辑中的标题同时存一份 ref：saveEdit 需要固定引用（否则每次击键都会换身份，
  // 让 memo 化的条目行全部重渲染），而回调里又必须读到最新文本。
  const editingTitleRef = useRef('');
  // 标记本次编辑是取消：避免卸载/blur 时又触发一次保存
  const cancelEditRef = useRef(false);

  const [searchQuery, setSearchQuery] = useState('');
  const [searchOpen, setSearchOpen] = useState(false);

  // 顶栏胶囊工具栏发起的搜索请求。用「渲染期校正 props → state」而不是 effect：
  // effect 里同步 setState 会多跑一轮渲染（而且这正是 oxlint 拦的写法）。
  // seen 的初值是 0：首次挂载时如果已经攒了一个请求（>0），也应该打开搜索。
  const [seenSearchRequest, setSeenSearchRequest] = useState(0);
  if (seenSearchRequest !== searchRequest) {
    setSeenSearchRequest(searchRequest);
    if (searchRequest > 0) setSearchOpen(true);
  }

  // 搜索框的 autoFocus 只在挂载那一次生效：搜索已经打开时再按 ⌘K，需要把焦点从
  // 消息输入框抢回来，否则快捷键看上去「什么都没发生」。
  const searchInputRef = useRef<HTMLInputElement>(null);
  // 关闭搜索后输入框会卸载，焦点必须交给一个仍然存在的控件，否则会掉到 <body> 上
  const searchToggleRef = useRef<HTMLButtonElement>(null);
  const asideRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (searchRequest === 0) return;
    searchInputRef.current?.focus();
  }, [searchRequest]);

  /** 搜索结果里当前可聚焦的行按钮（DOM 顺序 = 渲染顺序）。 */
  const resultRowButtons = useCallback((): HTMLElement[] => {
    const root = asideRef.current;
    if (!root) return [];
    return [...root.querySelectorAll<HTMLElement>('[data-session-id] > button[aria-label]')];
  }, []);

  /** 把焦点放到某条会话行上（搜索框按 ↓ 时用）。 */
  const focusSessionRow = useCallback(
    (sessionId: string): void => {
      const root = asideRef.current;
      if (!root) return;
      const row = [...root.querySelectorAll<HTMLElement>('[data-session-id]')].find(
        (element) => element.dataset.sessionId === sessionId,
      );
      row?.querySelector<HTMLElement>(':scope > button')?.focus();
    },
    [],
  );

  const [isSelectMode, setIsSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());

  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null);
  // 轻量错误条：App 传进来的实现是 async 的，失败若无处呈现就等于「界面零提示」
  const [actionError, setActionError] = useState<string | null>(null);

  const runningKey = useSyncExternalStore(subscribeRunningSessions, getRunningSessionsKey);
  const runningIds = useMemo(
    () => new Set(runningKey ? runningKey.split(',') : []),
    [runningKey],
  );

  // 搜索：标题同步过滤 + 正文异步扫库。候选集合固定为当前会话列表（墓碑会话不在里面）。
  const sessionIds = useMemo(() => sessions.map((session) => session.id), [sessions]);
  const search = useSessionContentSearch(searchQuery, sessionIds);

  const filteredSessions = useMemo(() => {
    if (search.tokens.length === 0) return sessions;
    return sessions.filter(
      (session) =>
        matchText(session.title || '', search.tokens) !== null || search.matches.has(session.id),
    );
  }, [sessions, search.tokens, search.matches]);

  // 按时间分组，长列表更好扫读
  const sessionGroups = useMemo(() => groupSessionsByDate(filteredSessions), [filteredSessions]);

  // 批量操作只作用于「可见且被选中」的集合：搜索藏起来的选中项不会被顺带删除，
  // 确认弹窗的数量也因此等于实际删除集合。
  const visibleSelectedIds = useMemo(
    () => visibleSelection(selectedIds, filteredSessions.map((session) => session.id)),
    [selectedIds, filteredSessions],
  );
  const isAllSelected =
    filteredSessions.length > 0 && visibleSelectedIds.size === filteredSessions.length;

  /** 接住注入回调的失败：直接调用 async 实现会产生 unhandled rejection。 */
  const runAction = useCallback(
    async (action: () => void | Promise<unknown>, failureMessage: string): Promise<boolean> => {
      try {
        await action();
        setActionError(null);
        return true;
      } catch (err) {
        console.error(`${failureMessage}:`, err);
        setActionError(failureMessage);
        return false;
      }
    },
    [],
  );

  // -------------------------------------------------------------------
  // 重命名
  // -------------------------------------------------------------------
  const startEdit = useCallback((session: SessionRecord) => {
    cancelEditRef.current = false;
    editingTitleRef.current = session.title;
    setEditingId(session.id);
    setEditingTitle(session.title);
  }, []);

  const handleEditingTitleChange = useCallback((title: string) => {
    editingTitleRef.current = title;
    setEditingTitle(title);
  }, []);

  const saveEdit = useCallback(
    (sessionId: string) => {
      if (cancelEditRef.current) {
        cancelEditRef.current = false;
        setEditingId(null);
        return;
      }
      const title = editingTitleRef.current.trim();
      setEditingId(null);
      if (!title) return;
      void runAction(() => onUpdateTitle(sessionId, title), '重命名失败，请重试');
    },
    [onUpdateTitle, runAction],
  );

  const cancelEdit = useCallback(() => {
    cancelEditRef.current = true;
    setEditingId(null);
  }, []);

  // -------------------------------------------------------------------
  // 多选
  // -------------------------------------------------------------------
  const toggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const exitSelectMode = useCallback(() => {
    setIsSelectMode(false);
    setSelectedIds(new Set());
  }, []);

  const handleTogglePin = useCallback(
    (id: string) => {
      void runAction(() => onTogglePinSession(id), '置顶操作失败，请重试');
    },
    [onTogglePinSession, runAction],
  );

  // 导出失败（IndexedDB 读不出来等）必须落到界面上：否则用户点了菜单，什么也没发生。
  const handleExport = useCallback(
    (session: SessionRecord) => {
      if (!onExportSession) return;
      void runAction(() => onExportSession(session.id), '导出失败，请重试');
    },
    [onExportSession, runAction],
  );

  const handleExportSelected = useCallback(() => {
    if (!onExportSessions) return;
    const ids = [...visibleSelectedIds];
    if (ids.length === 0) return;
    void runAction(() => onExportSessions(ids), '导出失败，请重试');
  }, [onExportSessions, visibleSelectedIds, runAction]);

  const handleBatchPin = useCallback(
    (pinned: boolean) => {
      const ids = [...visibleSelectedIds];
      if (ids.length === 0) return;
      void (async () => {
        const ok = await runAction(
          () => onBatchTogglePin(ids, pinned),
          pinned ? '置顶失败，请重试' : '取消置顶失败，请重试',
        );
        // 失败时保留多选态，用户可以直接重试
        if (ok) setSelectedIds(new Set());
      })();
    },
    [visibleSelectedIds, onBatchTogglePin, runAction],
  );

  // -------------------------------------------------------------------
  // 破坏性操作：先弹确认，再执行
  // -------------------------------------------------------------------
  const requestDeleteSession = useCallback(
    (session: SessionRecord) => {
      setConfirmRequest({
        title: '删除对话',
        description: `「${session.title}」及其全部消息将被删除，且无法恢复。`,
        confirmLabel: '删除',
        destructive: true,
        onConfirm: () => {
          void runAction(() => onDeleteSession(session.id), '删除失败，请重试');
        },
      });
    },
    [onDeleteSession, runAction],
  );

  const requestBatchDelete = useCallback(() => {
    const ids = [...visibleSelectedIds];
    if (ids.length === 0) return;
    setConfirmRequest({
      title: `删除 ${ids.length} 个对话`,
      description: '选中会话及其全部消息将被删除，且无法恢复。',
      confirmLabel: '删除',
      destructive: true,
      onConfirm: () => {
        void (async () => {
          const ok = await runAction(() => onBatchDelete(ids), '删除失败，请重试');
          // 失败时保留多选态，用户可以直接重试
          if (ok) exitSelectMode();
        })();
      },
    });
  }, [visibleSelectedIds, onBatchDelete, runAction, exitSelectMode]);

  const requestClearAll = useCallback(() => {
    setConfirmRequest({
      title: '清空所有历史对话',
      description: '全部会话与消息将被删除，且无法恢复。',
      confirmLabel: '全部清空',
      destructive: true,
      onConfirm: () => {
        void (async () => {
          const ok = await runAction(() => onClearAllSessions(), '清空失败，请重试');
          if (ok) exitSelectMode();
        })();
      },
    });
  }, [onClearAllSessions, runAction, exitSelectMode]);

  const handleCreateSession = useCallback(() => {
    void runAction(() => onCreateSession(), '新建会话失败，请重试');
  }, [onCreateSession, runAction]);

  // 收起后整块不渲染，入口由 ChatContainer 顶栏的胶囊工具栏接替。
  // 确认弹窗仍要挂载——收起前发起、收起后才确认的删除不能因侧边栏隐了而丢。
  const confirmDialog = (
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
  );

  if (!isOpen) return confirmDialog;

  return (
    <aside
      ref={asideRef}
      aria-label="历史对话侧边栏"
      className="w-64 shrink-0 flex flex-col h-full select-none bg-sidebar absolute md:relative inset-y-0 left-0 z-30"
    >
      <div className="px-3 pt-4 pb-3 space-y-3 shrink-0">
        {/* 品牌 + 顶部图标 */}
        <div className="flex items-center justify-between px-1">
          <span className="text-[15px] font-semibold tracking-tight text-foreground select-none">
            Bennett
          </span>
          <div className="flex items-center gap-0.5">
            <SyncStatusButton />
            <button
              ref={searchToggleRef}
              type="button"
              onClick={() => {
                setSearchOpen((open) => !open);
                if (searchOpen) setSearchQuery('');
              }}
              className="size-8 flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent/60 rounded-lg transition cursor-pointer"
              title={`搜索对话（${searchShortcutLabel()}）`}
              aria-label="搜索对话"
              aria-keyshortcuts={SEARCH_SHORTCUT_KEYS}
              aria-expanded={searchOpen}
            >
              <Search size={16} aria-hidden="true" />
            </button>
            {onToggleOpen && (
              <button
                type="button"
                onClick={onToggleOpen}
                className="size-8 flex items-center justify-center text-muted-foreground hover:text-foreground hover:bg-accent/60 rounded-lg transition cursor-pointer"
                title="收起侧边栏"
                aria-label="收起侧边栏"
              >
                {/* 方框 + 左竖线，与展开后的胶囊工具栏保持同一套视觉语言 */}
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.75"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <rect x="3" y="3" width="18" height="18" rx="3" />
                  <line x1="9" y1="3" x2="9" y2="21" />
                </svg>
              </button>
            )}
          </div>
        </div>

        {/* 搜索框：点图标才展开，平时只有图标，不占列表空间。
            搜的是「标题 + 消息正文」：标题同步过滤，正文结果由 useSessionContentSearch 异步补上。 */}
        {searchOpen && (
          <div className="space-y-1">
            <div className="relative flex items-center">
              <Search
                size={13}
                className="absolute left-3 text-muted-foreground pointer-events-none"
                aria-hidden="true"
              />
              <input
                ref={searchInputRef}
                type="text"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape') {
                    event.preventDefault();
                    setSearchQuery('');
                    setSearchOpen(false);
                    searchToggleRef.current?.focus();
                    return;
                  }
                  // Enter 打开第一条结果：搜索时手不用离开键盘。
                  if (event.key === 'Enter') {
                    const first = filteredSessions[0];
                    if (!first) return;
                    event.preventDefault();
                    onSelectSession(first.id);
                    return;
                  }
                  // ↓ 把焦点交进结果列表，之后用原生按钮的键盘行为继续操作。
                  if (event.key === 'ArrowDown') {
                    const first = filteredSessions[0];
                    if (!first) return;
                    event.preventDefault();
                    focusSessionRow(first.id);
                  }
                }}
                placeholder="搜索标题与对话内容..."
                aria-label="搜索历史会话（标题与消息内容）"
                autoFocus
                className="w-full bg-foreground/[0.04] rounded-xl pl-8 pr-14 py-2 text-base text-foreground placeholder:text-muted-foreground outline-none focus:ring-1 focus:ring-ring/30 md:text-xs"
              />
              {search.pending && (
                <Loader2
                  size={12}
                  className="absolute right-7 animate-spin text-muted-foreground"
                  aria-hidden="true"
                />
              )}
              <button
                type="button"
                onClick={() => {
                  setSearchQuery('');
                  setSearchOpen(false);
                  searchToggleRef.current?.focus();
                }}
                className="absolute right-2.5 text-muted-foreground hover:text-foreground p-0.5 cursor-pointer"
                title="关闭搜索"
              >
                <X size={12} aria-hidden="true" />
                <span className="sr-only">关闭搜索</span>
              </button>
            </div>
            {/* 结果数量对读屏播报；视觉上片段本身就说明了命中原因，不再占一行 */}
            <p role="status" className="px-1 text-[11px] text-muted-foreground">
              {search.pending
                ? '正在搜索…'
                : search.error
                  ? search.error
                  : search.tokens.length > 0
                    ? `${filteredSessions.length} 个对话`
                    : ''}
            </p>
          </div>
        )}

        {/* 开启新对话：全圆角胶囊 */}
        <button
          type="button"
          onClick={handleCreateSession}
          className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-[13px] font-medium rounded-full border border-border/70 bg-background hover:bg-accent/40 text-foreground transition cursor-pointer"
        >
          <Plus size={16} strokeWidth={2} aria-hidden="true" />
          <span>开启新对话</span>
        </button>
      </div>

      {actionError && (
        <div
          role="alert"
          className="mx-3 mb-1.5 flex items-start gap-1.5 rounded-xl border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-[11px] text-destructive"
        >
          <span className="flex-1">{actionError}</span>
          <button
            type="button"
            onClick={() => setActionError(null)}
            className="shrink-0 hover:opacity-70 cursor-pointer"
            title="关闭提示"
          >
            关闭
          </button>
        </div>
      )}

      {isSelectMode && (
        <SidebarBatchBar
          selectedCount={visibleSelectedIds.size}
          isAllSelected={isAllSelected}
          onToggleSelectAll={() =>
            setSelectedIds(isAllSelected ? new Set() : new Set(filteredSessions.map((s) => s.id)))
          }
          onExitSelectMode={exitSelectMode}
          onPinSelected={() => handleBatchPin(true)}
          onUnpinSelected={() => handleBatchPin(false)}
          onRequestBatchDelete={requestBatchDelete}
          onRequestClearAll={requestClearAll}
          onExportSelected={onExportSessions ? handleExportSelected : undefined}
          canClearAll={sessions.length > 0}
        />
      )}

      <div
        className="flex-1 overflow-y-auto pb-2"
        onKeyDown={(event) => {
          // 只在「搜索中」接管方向键：普通列表里 ↑↓ 应该滚动，而不是劫持焦点
          if (!searchOpen) return;
          const buttons = resultRowButtons();
          const index = buttons.indexOf(document.activeElement as HTMLElement);

          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            if (index === -1) return; // 焦点还在搜索框：由输入框自己处理 ↓
            event.preventDefault();
            buttons[index + (event.key === 'ArrowDown' ? 1 : -1)]?.focus();
            return;
          }

          // 在结果里按 Esc：先退回搜索框（搜索仍然开着），再按一次才关闭搜索。
          // 直接关闭会让输入框立刻卸载，焦点掉到 body 上——两段式 Esc 更符合预期。
          if (event.key === 'Escape' && index >= 0) {
            event.preventDefault();
            searchInputRef.current?.focus();
          }
        }}
      >
        {filteredSessions.length === 0 ? (
          <div className="text-xs text-muted-foreground text-center py-12">
            {search.pending
              ? '正在搜索对话内容…'
              : search.tokens.length > 0
                ? '未找到相关对话'
                : '暂无历史对话'}
          </div>
        ) : (
          sessionGroups.map((group, groupIndex) => (
            <section key={group.label}>
              <div className="flex items-center justify-between pl-4 pr-2.5 pt-4 pb-1">
                <h3 className="text-[11px] font-medium text-muted-foreground/70 select-none">
                  {group.label}
                </h3>
                {/* 多选管理入口挂在第一个分组标题右侧 */}
                {groupIndex === 0 && !searchOpen && (
                  <button
                    type="button"
                    onClick={() => {
                      setIsSelectMode((value) => !value);
                      setSelectedIds(new Set());
                    }}
                    title={isSelectMode ? '退出多选' : '多选管理'}
                    aria-pressed={isSelectMode}
                    className={`p-1 rounded-md transition cursor-pointer shrink-0 ${
                      isSelectMode
                        ? 'bg-accent text-accent-foreground'
                        : 'text-muted-foreground/60 hover:text-foreground hover:bg-accent/60'
                    }`}
                  >
                    <SlidersHorizontal size={13} aria-hidden="true" />
                    <span className="sr-only">{isSelectMode ? '退出多选' : '多选管理'}</span>
                  </button>
                )}
              </div>
              <div className="px-2 space-y-0.5">
                {group.sessions.map((session) => (
                  <SidebarSessionItem
                    key={session.id}
                    session={session}
                    isActive={currentSessionId === session.id && !isSelectMode}
                    isRunning={runningIds.has(session.id)}
                    isSelected={selectedIds.has(session.id)}
                    isSelectMode={isSelectMode}
                    isEditing={editingId === session.id}
                    editingTitle={editingId === session.id ? editingTitle : ''}
                    searchTokens={search.tokens}
                    contentMatch={search.matches.get(session.id) ?? null}
                    onEditingTitleChange={handleEditingTitleChange}
                    onStartEdit={startEdit}
                    onSaveEdit={saveEdit}
                    onCancelEdit={cancelEdit}
                    onSelect={onSelectSession}
                    onToggleSelect={toggleSelect}
                    onTogglePin={handleTogglePin}
                    onExport={onExportSession ? handleExport : undefined}
                    onRequestDelete={requestDeleteSession}
                  />
                ))}
              </div>
            </section>
          ))
        )}
      </div>

      <AccountBar
        email={userEmail}
        onLogout={onLogout}
        onOpenSettings={onOpenSettings}
        canOpenAdmin={canOpenAdmin}
        onOpenAdmin={onOpenAdmin}
      />

      {confirmDialog}
    </aside>
  );
}
