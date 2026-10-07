import React from 'react';
import { Thread } from './assistant-ui/elements/thread.aui';
import { PanelLeft, Plus } from 'lucide-react';

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
  onSubmitClarificationAnswer,
}) {
  return (
    <div className="flex-1 flex flex-col h-full bg-background text-foreground overflow-hidden">
      {/* 顶栏：左侧侧边栏开关，移动端右侧展示“新对话”按钮 */}
      <header className="h-12 border-b flex items-center justify-between px-3 z-10 shrink-0 bg-background/80 backdrop-blur select-none">
        <button
          onClick={onToggleSidebar}
          className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition cursor-pointer"
          title="切换侧边栏"
          aria-label="切换侧边栏"
        >
          <PanelLeft size={16} />
        </button>

        {/* 仅在移动端（md:hidden）展示右上角新建会话按钮 */}
        {onCreateSession && (
          <button
            onClick={onCreateSession}
            className="md:hidden flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-md border border-border/80 hover:bg-accent hover:text-accent-foreground text-foreground transition cursor-pointer"
            title="新建对话"
            aria-label="新建对话"
          >
            <Plus size={14} />
            <span>新对话</span>
          </button>
        )}
      </header>

      {/* 对话区域：模型选择与思考等级直接集成在输入框底部 */}
      <div className="flex-1 overflow-hidden">
        <Thread
          models={models}
          modelsError={modelsError}
          currentModel={currentModel}
          currentReasoningEffort={currentReasoningEffort}
          onSelectModel={onSelectModel}
          onSelectReasoningEffort={onSelectReasoningEffort}
          onRefreshModels={onRefreshModels}
          onSubmitClarificationAnswer={onSubmitClarificationAnswer}
        />
      </div>
    </div>
  );
}
