import { useCallback, useRef, useState } from 'react';
import { Check, Pencil, Plus, X } from 'lucide-react';

import { Thread } from './assistant-ui/elements/thread.aui';
import { clampTitle, titleLength, TITLE_MAX_LENGTH } from '../lib/session-list';
import { notify } from '../lib/toast';
import { ChatSessionNotices } from './ChatProvider';
import type { ModelItem } from '../types';

export interface ChatContainerProps {
  sessionTitle?: string;
  onUpdateTitle?: (title: string) => void | Promise<unknown>;
  models: ModelItem[];
  modelsError?: string | null;
  /** 服务端是否装配了联网搜索（`/api/capabilities`）；用于裁剪空对话页的起步建议。 */
  webSearchAvailable?: boolean;
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
 * **只有「展开侧边栏」与「新建对话」两个按钮**。
 *
 * 搜索 / 设置 / 同步 / 账号都不再放进胶囊：它们本来就有侧边栏、抽屉这两处常驻入口，
 * 挤进顶栏只是把标题压没（移动端曾把 6 个图标排成一行）。收起侧边栏后要找回它们，
 * 展开侧边栏即可，快捷键也不受影响（⌘K 搜索、⌘, 设置）。
 *
 * 标题重命名：输入框按内容自动加宽（`field-sizing: content`，不支持的浏览器退回 size
 * 属性），否则长标题在 12rem 的固定宽度里只能看见一小截。**保存失败时不关闭编辑器**：
 * 文本留在原地、错误就显示在旁边，用户可以按 Enter 重试；关掉编辑器再默默回退标题，
 * 等于把失败藏进控制台。
 */
export function ChatContainer({
  sessionTitle,
  onUpdateTitle,
  models,
  modelsError,
  webSearchAvailable,
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
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [prevSessionTitle, setPrevSessionTitle] = useState(sessionTitle);
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelEditRef = useRef(false);
  // 在飞标记用 ref：blur 与 submit 可能在同一轮事件里先后触发，state 还没更新，
  // 只看 `saving` 会把同一次保存写两遍（第二次还会被当成失败重试的入口）。
  const savingRef = useRef(false);
  // 取消/重新开始编辑会换一个 token：在途保存回来时据此丢弃结果，不把已经取消的编辑器
  // 又打开、也不弹一条属于上一次编辑的错误。
  const saveTokenRef = useRef(0);

  if (prevSessionTitle !== sessionTitle) {
    setPrevSessionTitle(sessionTitle);
    if (!isEditing) {
      setEditingTitle(sessionTitle || '新对话');
    }
  }

  /**
   * 新建对话必须接住 rejection。
   *
   * 直接把 async 的 onCreateSession 挂在 onClick 上，失败就是一个没人处理的 Promise：
   * 点了没反应、控制台里也没有归属，用户只会再点几次。这里给一条错误提示。
   */
  const handleCreateSession = useCallback(() => {
    if (!onCreateSession) return;
    void Promise.resolve(onCreateSession()).catch((err: unknown) => {
      console.error('新建会话失败:', err);
      notify('error', '新建会话失败，请重试');
    });
  }, [onCreateSession]);

  const handleStartEdit = useCallback(() => {
    cancelEditRef.current = false;
    setSaveError(null);
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
      if (savingRef.current) return;

      const trimmed = editingTitle.trim();
      if (!trimmed || trimmed === sessionTitle) {
        setSaveError(null);
        setIsEditing(false);
        return;
      }

      const token = saveTokenRef.current + 1;
      saveTokenRef.current = token;
      savingRef.current = true;
      setSaving(true);
      setSaveError(null);

      try {
        await onUpdateTitle?.(trimmed);
        if (token === saveTokenRef.current) setIsEditing(false);
      } catch (err) {
        console.error('修改会话标题失败:', err);
        // 不关闭编辑器：文本与错误都在眼前，按 Enter 就是重试。
        if (token === saveTokenRef.current) setSaveError('标题保存失败，请重试');
      } finally {
        if (token === saveTokenRef.current) {
          savingRef.current = false;
          setSaving(false);
        }
      }
    },
    [editingTitle, sessionTitle, onUpdateTitle]
  );

  const handleCancelEdit = useCallback(() => {
    cancelEditRef.current = true;
    // 让在途保存的结算全部失效（它既不该关闭编辑器，也不该弹错误）
    saveTokenRef.current += 1;
    savingRef.current = false;
    setSaving(false);
    setSaveError(null);
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
                  onClick={handleCreateSession}
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
                onChange={(e) => {
                  // 不用 maxLength 属性：它在浏览器层面静默截断粘贴内容，用户不知道
                  // 自己丢了字。这里截断并明确提示。
                  const clamped = clampTitle(e.target.value);
                  if (clamped.truncated) {
                    notify('info', `标题最多 ${TITLE_MAX_LENGTH} 字，超出部分已忽略`);
                  }
                  setEditingTitle(clamped.title);
                }}
                onBlur={(event) => {
                  // 焦点在编辑器内部移动（Tab 到保存/取消）不算「离开」，不能抢跑保存。
                  // 否则键盘用户一 Tab 就被自动保存，永远到不了取消按钮。
                  const next = event.relatedTarget;
                  if (next instanceof Node && event.currentTarget.form?.contains(next)) return;
                  void handleSaveTitle();
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    handleCancelEdit();
                    return;
                  }
                  // 输入法组合中按 Enter 是「选词」，不能当成保存：让 form 的 submit
                  // 不发生，否则中文用户选完候选词标题就被存了半截。
                  if (e.key === 'Enter' && (e.nativeEvent.isComposing || e.keyCode === 229)) {
                    e.preventDefault();
                  }
                }}
                /* size 是不支持 field-sizing 的浏览器（Firefox 旧版等）的退路；
                   支持时它被 content 尺寸覆盖 */
                size={Math.min(Math.max(12, titleLength(editingTitle)), 48)}
                className="h-7 min-w-32 max-w-[min(80vw,28rem)] px-2.5 text-base font-medium bg-background border border-ring/40 rounded-lg outline-none focus:ring-1 focus:ring-ring/40 text-foreground md:text-sm [field-sizing:content]"
                placeholder="输入会话标题..."
                aria-invalid={saveError ? true : undefined}
                aria-describedby={saveError ? 'title-save-error' : undefined}
                autoFocus
              />
              {titleLength(editingTitle) >= 80 && (
                <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                  {titleLength(editingTitle)}/{TITLE_MAX_LENGTH}
                </span>
              )}
              <button
                type="submit"
                disabled={saving}
                className="p-1 text-muted-foreground hover:text-foreground rounded-md transition cursor-pointer disabled:opacity-50 disabled:cursor-default focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
                title="保存"
                aria-label="保存"
              >
                <Check className="size-3.5 text-primary" />
              </button>
              <button
                type="button"
                onClick={handleCancelEdit}
                className="p-1 text-muted-foreground hover:text-foreground rounded-md transition cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
                title="取消"
                aria-label="取消"
              >
                <X className="size-3.5" />
              </button>
              {saveError && (
                <span
                  id="title-save-error"
                  role="alert"
                  className="shrink-0 whitespace-nowrap text-[11px] text-destructive"
                >
                  {saveError}
                </span>
              )}
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
          webSearchAvailable={webSearchAvailable}
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
