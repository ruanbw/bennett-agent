import React, { useRef, useState, useSyncExternalStore } from 'react';
import {
  Plus,
  MessageSquare,
  Loader2,
  Edit2,
  Trash2,
  Sun,
  Moon,
  Laptop,
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
  isOpen,
}) {
  const [editingId, setEditingId] = useState(null);
  const [editingTitle, setEditingTitle] = useState('');
  // 标记本次编辑是取消：避免卸载/blur 时又触发一次保存
  const cancelEditRef = useRef(false);

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

  return (
    <aside
      className={`w-64 border-r flex flex-col h-full select-none transition-transform duration-200 z-30 shrink-0 bg-muted/20 absolute md:relative inset-y-0 left-0 ${
        isOpen ? 'translate-x-0' : '-translate-x-full md:hidden'
      }`}
    >
      {/* 头部：新建对话 */}
      <div className="p-3">
        <button
          onClick={onCreateSession}
          className="w-full flex items-center justify-center gap-2 px-3 py-2 text-xs font-medium rounded-md border hover:bg-accent hover:text-accent-foreground transition cursor-pointer"
        >
          <Plus size={14} />
          <span>新对话</span>
        </button>
      </div>

      {/* 会话列表 */}
      <div className="flex-1 overflow-y-auto p-2 space-y-0.5">
        {sessions.length === 0 ? (
          <div className="text-xs text-muted-foreground text-center py-8">
            暂无历史对话
          </div>
        ) : (
          sessions.map((session) => (
            <div
              key={session.id}
              onClick={() => onSelectSession(session.id)}
              className={`group relative flex items-center gap-2 px-3 py-2 rounded-md text-xs cursor-pointer transition-colors ${
                currentSessionId === session.id
                  ? 'bg-accent text-accent-foreground font-medium'
                  : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
              }`}
            >
              {runningIds.includes(session.id) ? (
                <Loader2 size={14} className="shrink-0 animate-spin text-foreground/70" />
              ) : (
                <MessageSquare size={14} className="shrink-0" />
              )}

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
                <span className="flex-1 truncate">{session.title}</span>
              )}

              {editingId !== session.id && (
                <div className="opacity-0 group-hover:opacity-100 flex items-center gap-1 transition-opacity shrink-0">
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
          ))
        )}
      </div>

      {/* 底部左下角：三段式主题切换器 */}
      <div className="p-3 border-t border-border/60">
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
