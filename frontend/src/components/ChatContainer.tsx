import { PanelLeft, Plus } from 'lucide-react';

import { Thread } from './assistant-ui/elements/thread.aui';
import { ChatSessionNotices } from './ChatProvider';
import type { ModelItem } from '../types';

export interface ChatContainerProps {
  models: ModelItem[];
  modelsError?: string | null;
  currentModel: string | null;
  currentReasoningEffort?: string;
  onSelectModel: (modelId: string) => void;
  onSelectReasoningEffort?: (effort: string) => void;
  onRefreshModels?: () => void | Promise<unknown>;
  onToggleSidebar: () => void;
  // 既当 onClick 用，也（在 App 层）传给 Sidebar；两处都接受同步或异步实现
  onCreateSession?: () => void | Promise<unknown>;
}

/**
 * 对话主区域：顶栏 + Thread。
 *
 * 不再转发提问相关的 prop——那三个 prop 原本由 ChatProvider 通过 `cloneElement`
 * 注入、再经这里原样传给 Thread，是一层看不见的耦合。现在 Thread 直接从
 * `clarification-context` 取。
 */
export function ChatContainer({
  models,
  modelsError,
  currentModel,
  currentReasoningEffort,
  onSelectModel,
  onSelectReasoningEffort,
  onRefreshModels,
  onToggleSidebar,
  onCreateSession,
}: ChatContainerProps) {
  return (
    <div className="flex-1 flex flex-col h-full bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center justify-between px-3 z-10 shrink-0 bg-background/80 backdrop-blur select-none">
        <button
          type="button"
          onClick={onToggleSidebar}
          className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition cursor-pointer"
          title="切换侧边栏"
          aria-label="切换侧边栏"
        >
          <PanelLeft size={16} aria-hidden="true" />
        </button>

        {/* 仅在移动端（md:hidden）展示右上角新建会话按钮 */}
        {onCreateSession && (
          <button
            type="button"
            onClick={onCreateSession}
            className="md:hidden flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-md border border-border/80 hover:bg-accent hover:text-accent-foreground text-foreground transition cursor-pointer"
            title="新建对话"
            aria-label="新建对话"
          >
            <Plus size={14} aria-hidden="true" />
            <span>新对话</span>
          </button>
        )}
      </header>

      {/*
        会话级提示条（发送未成功 / 本轮回复未能落库）。放在对话区顶部而不是塞进消息流：
        它描述的是「这一轮的状态」，不是某条消息的内容，也绝不能挡住后续发送。
      */}
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
        />
      </div>
    </div>
  );
}
